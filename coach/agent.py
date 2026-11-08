"""The coach agent: an LLM in a tool loop over Stockfish, with a verify-then-repair pass."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import chess

from . import prompts
from .config import Settings
from .context import build_context, moment_block
from .engine import EnginePool, analyse_lines, fmt_eval
from .llm import LLMError, LLMResponse, Provider, ToolCall, ToolSpec
from .motifs import TAG_LABELS
from .session import CoachSession, _line_fens, clean_san
from .verify import Evidence, problems_for_repair, verify

REF_HELP = "A move reference like 'G1 22...Qc7' (Black's 22nd move in game G1) or 'G3 14.Nf3' (White's 14th move)."

TOOLS = [
    ToolSpec(
        "get_game_moves",
        "Move list of one game with clock times and the engine's verdict on each move (evals from the coached player's side).",
        {
            "type": "object",
            "properties": {
                "game_id": {"type": "string", "description": "Game id such as 'G2'."},
                "from_move": {"type": "integer", "description": "First full-move number to include (default 1)."},
                "to_move": {"type": "integer", "description": "Last full-move number to include (default from_move + 29)."},
            },
            "required": ["game_id"],
        },
    ),
    ToolSpec(
        "get_moment",
        "Full engine detail for one move: evals before/after, the engine's top choices, the opponent's best reply, clock, patterns.",
        {"type": "object", "properties": {"ref": {"type": "string", "description": REF_HELP}}, "required": ["ref"]},
    ),
    ToolSpec(
        "analyze_position",
        "Run Stockfish on the position just before (default) or just after a given move and return its top 3 lines.",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": REF_HELP},
                "when": {"type": "string", "enum": ["before", "after"], "description": "Position before or after that move."},
            },
            "required": ["ref"],
        },
    ),
    ToolSpec(
        "evaluate_move",
        "Test an alternative move in the position where a given move was played: is it legal, how does the engine rate it "
        "compared with the best move and with the move actually played, and what is the critical reply.",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": REF_HELP + " The alternative is tried in the position BEFORE this move."},
                "move": {"type": "string", "description": "The alternative move in SAN, e.g. 'Nf3' or 'exd5'."},
            },
            "required": ["ref", "move"],
        },
    ),
]


def _clock(s: float | None) -> str:
    return "" if s is None else f"{int(s // 60)}:{int(s % 60):02d}"


def _to_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class Toolbox:
    def __init__(self, session: CoachSession, pool: EnginePool, settings: Settings):
        self.s, self.pool, self.settings = session, pool, settings
        self.evidence = Evidence()

    def call(self, tc: ToolCall) -> str:
        fn = {
            "get_game_moves": self.get_game_moves,
            "get_moment": self.get_moment,
            "analyze_position": self.analyze_position,
            "evaluate_move": self.evaluate_move,
        }.get(tc.name)
        if not fn:
            return f"Unknown tool '{tc.name}'."
        try:
            out = fn(**tc.arguments)
        except TypeError as exc:
            out = f"Bad arguments for {tc.name}: {exc}"
        except Exception as exc:  # tools must never crash the conversation
            out = f"Tool {tc.name} failed: {exc}"
        self.evidence.texts.append(out)
        return out

    def _resolve(self, ref: str):
        t = self.s.resolve_ref(str(ref or ""))
        if t is None:
            raise ValueError(f"'{ref}' does not match any move in these games. Use the format 'G1 22...Qc7' or 'G1 22...'.")
        return t

    def _add_line_fens(self, start_fen: str, ucis: list[str]) -> None:
        self.evidence.fens.update(_line_fens(start_fen, ucis))

    # -- tools ------------------------------------------------------------------------------
    def get_game_moves(self, game_id: str, from_move: int = 1, to_move: int | None = None) -> str:
        g = self.s.game(str(game_id))
        if not g:
            return f"No game '{game_id}'. Games are " + ", ".join(x.gid for x in self.s.games)
        a = self.s.analyses[g.gid]
        # Models sometimes send numbers as strings; be forgiving rather than failing the call.
        from_move = max(1, _to_int(from_move, 1))
        to_move = _to_int(to_move, 0) or from_move + 29
        if to_move < from_move:
            from_move, to_move = to_move, from_move
        rows = [
            f"{g.gid}: {g.white} (White) vs {g.black} (Black), {g.player} played {g.player_color}, result for {g.player}: "
            f"{g.outcome} ({g.end_reason}). Evals are from {g.player}'s side."
        ]
        for p in g.plies:
            if not (from_move <= p.move_number <= to_move):
                continue
            mv = a.moves[p.index]
            ev = a.evals[p.index + 1]
            cp, mate = ev.pov(g.player_color)
            evs = "mate" if ev.terminal == "checkmate" else ("draw" if ev.terminal == "draw" else fmt_eval(cp, mate))
            extra = ""
            if mv.classification in ("inaccuracy", "mistake", "miss", "blunder"):
                extra = f" {mv.classification}" + (f" (engine: {mv.best_san})" if mv.best_san else "")
            clk = f" [{_clock(p.clock)}]" if p.clock is not None else ""
            rows.append(f"{p.label}{clk} {evs}{extra}")
            self.evidence.fens.add(p.fen_before)
            self.evidence.fens.add(p.fen_after)
        if len(rows) == 1:
            rows.append("(no moves in that range)")
        return "\n".join(rows)

    def get_moment(self, ref: str) -> str:
        t = self._resolve(ref)
        g = t.game
        self.evidence.fens.update(self.s.evidence_fens(t))
        moment = self.s.moment_at(g.gid, t.ply.index)
        if moment:
            return moment_block(moment, g)
        mv = self.s.analyses[g.gid].moves[t.ply.index]
        board = chess.Board(t.ply.fen_before)
        with self.pool.engine() as eng:
            lines = analyse_lines(eng, board, t.ply.color, self.settings.deep_nodes, multipv=3, game=t.game.uuid)
        for l in lines:
            self._add_line_fens(t.ply.fen_before, l.uci)
        out = [
            f"{g.gid} {t.ply.label} ({t.ply.color}) | {mv.phase} | verdict: {mv.classification} | clock after move {_clock(t.ply.clock) or 'n/a'}",
            f"  position before (FEN): {t.ply.fen_before}",
            f"  eval before {mv.eval_before} ({mv.win_before:.0f}% win chance) -> after: {mv.eval_after} ({mv.win_after:.0f}%) "
            f"(evals from {t.ply.color}'s side)",
        ]
        if lines:
            out.append("  engine's top choices: " + "; ".join(f"{i}) {l.san[0]} {l.eval_str} [line: {' '.join(l.san)}]" for i, l in enumerate(lines, 1)))
        return "\n".join(out)

    def analyze_position(self, ref: str, when: str = "before") -> str:
        t = self._resolve(ref)
        fen = t.ply.fen_after if when == "after" else t.ply.fen_before
        board = chess.Board(fen)
        pov = t.game.player_color
        if board.is_game_over():
            return f"The position {when} {t.ply.label} is game over ({board.result()})."
        with self.pool.engine() as eng:
            lines = analyse_lines(eng, board, pov, self.settings.deep_nodes, multipv=3, game=t.game.uuid)
        self.evidence.fens.add(fen)
        for l in lines:
            self._add_line_fens(fen, l.uci)
        to_move = "White" if board.turn == chess.WHITE else "Black"
        out = [f"Position {when} {t.game.gid} {t.ply.label} ({to_move} to move). FEN: {fen}. Evals from {t.game.player}'s side."]
        for i, l in enumerate(lines, 1):
            out.append(f"{i}) {l.san[0]} {l.eval_str}: {' '.join(l.san)}")
        return "\n".join(out)

    def evaluate_move(self, ref: str, move: str) -> str:
        t = self._resolve(ref)
        board = chess.Board(t.ply.fen_before)
        mover = t.ply.color
        move = str(move or "")
        try:
            mv = board.parse_san(clean_san(move))
            if mv not in board.legal_moves:  # parse_san accepts the null move "--"
                raise ValueError(move)
        except ValueError:
            legal = ", ".join(sorted(board.san(m) for m in board.legal_moves))
            return f"'{move}' is not a legal move in the position before {t.ply.label}. Legal moves: {legal}"
        san = board.san(mv)
        moment = self.s.moment_at(t.game.gid, t.ply.index)
        with self.pool.engine() as eng:
            lines = moment.best_lines if moment and moment.best_lines else analyse_lines(eng, board, mover, self.settings.deep_nodes, multipv=3, game=t.game.uuid)
            cand = next((l for l in lines if l.uci and l.uci[0] == mv.uci()), None)
            after = board.copy()
            after.push(mv)
            reply_line = None
            if after.is_checkmate():
                cand_win, cand_eval = 100.0, "#"
            elif cand is not None:
                cand_win, cand_eval = cand.win, cand.eval_str
            else:
                replies = analyse_lines(eng, after, mover, self.settings.deep_nodes, multipv=1, game=t.game.uuid)
                reply_line = replies[0] if replies else None
                cand_win = reply_line.win if reply_line else 50.0
                cand_eval = reply_line.eval_str if reply_line else "+0.0"
        self.evidence.fens.update({t.ply.fen_before, after.fen()})
        for l in lines:
            self._add_line_fens(t.ply.fen_before, l.uci)
        if reply_line:
            self._add_line_fens(after.fen(), reply_line.uci)
        best = lines[0] if lines else None
        played = self.s.analyses[t.game.gid].moves[t.ply.index]
        drop = (best.win - cand_win) if best else 0.0
        if drop < 5.0:
            self.evidence.endorsed.add((t.ply.fen_before, clean_san(san)))
        verdict = (
            "this IS the engine's top choice" if best and best.uci and best.uci[0] == mv.uci()
            else ("about as good as the engine's best (within 5 points)" if drop < 5 else f"worse than the engine's best by {drop:.0f} points of win chance")
        )
        vs_played = cand_win - played.win_after
        cmp = "the same as the move played" if clean_san(san) == clean_san(t.ply.san) else (
            f"{abs(vs_played):.0f} points {'better' if vs_played > 0 else 'worse'} than the game move {t.ply.san}"
        )
        out = [
            f"Candidate {san} in the position before {t.game.gid} {t.ply.label} ({mover} to move), evals from {mover}'s side:",
            f"  after {san}: {cand_eval} ({cand_win:.0f}% win chance); {verdict}; {cmp}.",
        ]
        if best:
            out.append(f"  engine best: {best.san[0]} {best.eval_str} [line: {' '.join(best.san)}]")
        if cand and len(cand.san) > 1:
            out.append(f"  expected continuation: {' '.join(cand.san)}")
        if reply_line and reply_line.san:
            out.append(f"  critical reply: {' '.join(reply_line.san)}")
        return "\n".join(out)


@dataclass
class AgentResult:
    text: str
    trace: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    rounds: int = 0


OnEvent = Callable[[dict], None]


def run_agent(
    provider: Provider, system: str, history: list[dict], toolbox: Toolbox, max_rounds: int, use_tools: bool = True, on_event: OnEvent | None = None
) -> AgentResult:
    """LLM <-> tool loop. With `on_event`, text is streamed as {"type": "delta"} events, each model call starts
    with {"type": "round"} (the UI clears its draft) and tool calls are announced as {"type": "tool"}."""
    history = list(history)
    res = AgentResult(text="")
    tools = TOOLS if use_tools else []
    max_rounds = max(0, max_rounds)
    for round_no in range(max_rounds + 1):
        # Last round: the model has to answer in prose. The provider is told not to call tools
        # (tool_choice none) and the history says why, so the answer never comes back empty.
        final = round_no == max_rounds
        if final and tools and round_no > 0:
            history.append({"role": "user", "text": "Tool budget used up. Answer now using only what you already have."})
        if on_event and hasattr(provider, "stream"):
            on_event({"type": "round"})
            resp: LLMResponse = provider.stream(system, history, tools, lambda t: on_event({"type": "delta", "text": t}), force_text=final)
        else:
            resp = provider.complete(system, history, tools, force_text=final)
        res.rounds += 1
        res.input_tokens += resp.input_tokens
        res.output_tokens += resp.output_tokens
        history.append({"role": "assistant", "text": resp.text, "tool_calls": resp.tool_calls})
        if not resp.tool_calls or final:
            res.text = resp.text.strip()
            return res
        results = []
        for tc in resp.tool_calls:
            if on_event:
                on_event({"type": "tool", "name": tc.name, "args": tc.arguments})
            t0 = time.time()
            out = toolbox.call(tc)
            res.trace.append({"tool": tc.name, "args": tc.arguments, "result": out[:1200], "ms": round((time.time() - t0) * 1000)})
            results.append({"id": tc.id, "name": tc.name, "content": out})
        history.append({"role": "tool", "results": results})
    return res


def _respond(
    session: CoachSession,
    provider: Provider,
    pool: EnginePool,
    settings: Settings,
    system: str,
    history: list[dict],
    repair: bool = True,
    on_event: OnEvent | None = None,
) -> dict:
    t0 = time.time()
    context = build_context(session)
    toolbox = Toolbox(session, pool, settings)
    first = run_agent(provider, system, history, toolbox, settings.max_tool_rounds, on_event=on_event)
    if on_event:
        on_event({"type": "status", "text": "Checking every move, citation and evaluation against Stockfish"})
    check = verify(first.text, session, context, toolbox.evidence)
    out = {
        "markdown": first.text,
        "verification": check,
        "verification_before_repair": None,
        "repaired": False,
        "trace": first.trace,
        "usage": {"input_tokens": first.input_tokens, "output_tokens": first.output_tokens, "rounds": first.rounds},
    }
    if repair and check["flagged"]:
        if on_event:
            n = len(check["flagged"])
            on_event({"type": "status", "text": f"{n} claim{'s' if n != 1 else ''} did not check out. The coach is revising them"})
        fix_history = history + [
            {"role": "assistant", "text": first.text, "tool_calls": []},
            {"role": "user", "text": prompts.REPAIR_TEMPLATE.format(problems=problems_for_repair(check))},
        ]
        second = run_agent(provider, system, fix_history, toolbox, max(2, settings.max_tool_rounds // 2), on_event=on_event)
        if second.text:
            check2 = verify(second.text, session, context, toolbox.evidence)
            if check2["score"] >= check["score"]:
                out.update(markdown=second.text, verification=check2, verification_before_repair=check, repaired=True)
            out["trace"] = first.trace + second.trace
            out["usage"]["input_tokens"] += second.input_tokens
            out["usage"]["output_tokens"] += second.output_tokens
            out["usage"]["rounds"] += second.rounds
    out.update(mode="llm", provider=provider.name, model=provider.model, latency_s=round(time.time() - t0, 1))
    return out


def generate_report(
    session: CoachSession, provider: Provider | None, pool: EnginePool, settings: Settings, on_event: OnEvent | None = None
) -> dict:
    context = build_context(session)
    if provider is None:
        md = template_report(session)
        return {
            "markdown": md,
            "verification": verify(md, session, context),
            "verification_before_repair": None,
            "repaired": False,
            "trace": [],
            "usage": {},
            "mode": "template",
            "provider": None,
            "model": None,
            "latency_s": 0.0,
        }
    system = prompts.REPORT_SYSTEM + "\n\n" + prompts.context_message(context)
    return _respond(session, provider, pool, settings, system, [{"role": "user", "text": prompts.REPORT_TASK}], on_event=on_event)


def chat(
    session: CoachSession, provider: Provider | None, pool: EnginePool, settings: Settings, message: str, on_event: OnEvent | None = None
) -> dict:
    message = (message or "").strip()[:2000]
    if provider is None:
        return {
            "markdown": "Chat needs an LLM. Add `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) to `.env` and restart. "
            "Everything else on this page (engine analysis, critical moments, the template report) works without one.",
            "verification": None,
            "trace": [],
            "mode": "template",
        }
    context = build_context(session)
    system = prompts.CHAT_SYSTEM + "\n\n" + prompts.context_message(context)
    if session.report and session.report.get("markdown"):
        system += "\n\nREPORT YOU ALREADY GAVE THIS PLAYER\n" + session.report["markdown"]
    history = session.chat_history[-12:] + [{"role": "user", "text": message}]
    result = _respond(session, provider, pool, settings, system, history, on_event=on_event)
    session.chat_history.append({"role": "user", "text": message})
    session.chat_history.append({"role": "assistant", "text": result["markdown"], "tool_calls": []})
    return result


# -------------------------------------------------------------------------------------------------
# Engine-only fallback (no API key): deterministic, fully grounded by construction.
# -------------------------------------------------------------------------------------------------
DRILLS = {
    "missed_punish": "After every opponent move, ask what it allowed: list the checks, captures and threats it created before continuing your plan.",
    "hangs_piece": "Blunder check before every move: ask \"what does this leave undefended?\" Puzzles: Hanging Piece theme, 15 a day.",
    "loses_material": "Before committing, look at the opponent's forcing replies (checks, captures, threats). Puzzles: mixed tactics, 15 a day.",
    "allows_fork": "Puzzles: Fork theme, 15 a day. Notice when your king and a valuable piece sit a knight's jump apart.",
    "allows_mate": "Puzzles: Mate in 2 and Mate in 3, 10 a day, to spot mating nets around your own king earlier.",
    "missed_mate": "Puzzles: Mate in 2 and Mate in 3, 10 a day. In attacking positions, check every forcing move first.",
    "missed_material_gain": "Scan checks, captures and threats for BOTH sides on every move. Puzzles: Advantage theme.",
    "best_was_capture": "When a trade or capture is available, calculate it before playing a quiet move.",
    "best_was_check": "Always list your checks first, even ones that look silly.",
    "time_pressure": "Clock rule: keep at least 30 seconds for the final 10 moves; play quick, safe moves earlier in quiet positions.",
    "played_quickly": "Slow down at critical moments: after any capture, check or pawn break, spend at least 5 seconds.",
    "long_think": "Cap long thinks: if you have been thinking for 30+ seconds, pick the best candidate you have and move.",
    "was_winning": "Converting: when clearly winning, trade pieces, remove counterplay and stop calculating sharp lines.",
}


def _cap(text: str) -> str:
    """Capitalise the first letter only (str.capitalize() would lowercase SAN like 'Bxb2')."""
    return text[:1].upper() + text[1:]


def template_report(session: CoachSession) -> str:
    p = session.profile
    smp, acc, err = p["sample"], p["accuracy"], p["errors"]
    r = smp["record"]
    moments = sorted(session.moments(), key=lambda m: m.win_drop, reverse=True)
    lines = [
        "_Engine-only report (no LLM API key configured). Every statement below is generated directly from the "
        "Stockfish analysis. Add an API key for a personalised coach and chat._",
        "",
        "## The big picture",
        f"Across your last {smp['games']} games ({r['win']}W {r['loss']}L {r['draw']}D) your engine accuracy was "
        f"{acc['overall']}"
        + (f", lowest in the {acc['worst_phase']} ({acc['by_phase'][acc['worst_phase']]})" if acc.get("worst_phase") else "")
        + f". You averaged {err['per_game']['mistake']} mistakes, {err['per_game']['miss']} misses and {err['per_game']['blunder']} blunders per game.",
        "",
        "## Patterns to fix",
    ]
    pats = [x for x in p["patterns"] if x["tag"] not in ("best_was_check", "was_winning")]
    if not pats:
        lines.append("No recurring tactical pattern stood out in these games.")
    for x in pats[:3]:
        refs = ", ".join(f"[[{ref}]]" for ref in x["refs"][:4])
        lines.append(f"- **{_cap(x['label'])}** ({x['count']}x): {refs}")
    lines += ["", "## Moments to replay"]
    for m in moments[:5]:
        best = m.best_lines[0].san[0] if m.best_lines else None
        why = f" {_cap(m.tag_notes[0])}." if m.tag_notes else ""
        lines.append(
            f"- [[{m.ref}]]: a {m.classification} that took you from {m.eval_before} to {m.eval_after}."
            + (f" The engine preferred {best}." if best else "")
            + why
        )
    lines += ["", "## What is working"]
    phases = [(k, v) for k, v in acc["by_phase"].items() if v is not None]
    if phases:
        best_phase = max(phases, key=lambda kv: kv[1])
        lines.append(f"- Your {best_phase[0]} play was your most accurate phase ({best_phase[1]}).")
    if p["conversion"]["thrown_wins"]:
        lines.insert(lines.index("## Moments to replay") - 1, f"- **Letting wins slip**: you reached a winning position but did not win in " + ", ".join(p["conversion"]["thrown_wins"]) + ".")
    if p["conversion"]["comebacks"]:
        lines.append("- You fought back from a losing position in " + ", ".join(p["conversion"]["comebacks"]) + ".")
    good_open = [o for o in p["openings"] if o["games"] >= 1 and (o["score"] or 0) >= 50]
    if good_open:
        o = good_open[0]
        lines.append(f"- As {o['color']} in the {o['opening']} you scored {o['score']}% ({', '.join(o['gids'])}).")
    lines += ["", "## Your plan for the next 7 days"]
    seen = 0
    for x in pats:
        if x["tag"] in DRILLS and seen < 4:
            lines.append(f"- {DRILLS[x['tag']]}")
            seen += 1
    if acc.get("worst_phase") == "endgame":
        lines.append("- Endgames: 20 minutes a day on basic rook endgames and king activity.")
    if seen == 0:
        lines.append("- Review each moment above on the board and find the engine's idea before revealing it.")
    return "\n".join(lines)


__all__ = ["TOOLS", "Toolbox", "run_agent", "generate_report", "chat", "template_report", "LLMError", "TAG_LABELS"]
