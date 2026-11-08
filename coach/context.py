"""Builds the text the LLM reasons over.

Design rules (see DESIGN.md):
  * Facts, not raw data. The model gets engine verdicts, lines and pre-computed aggregates, never a
    bare PGN it would have to "read" (LLMs misread board states).
  * Stable IDs. Every game is G<n> and every move is citable as [[G<n> <move>]], so claims can be
    checked mechanically after generation.
  * One point of view. Evals and win% are always from the coached player's side.
  * Bounded size. At most MAX_MOMENTS critical moments go in the prompt; the rest are reachable
    through tools.
"""

from __future__ import annotations

from .engine import CriticalMoment
from .motifs import TAG_LABELS
from .pgn import ParsedGame
from .session import CoachSession

MAX_MOMENTS = 24


def _clock(s: float | None) -> str:
    if s is None:
        return "n/a"
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _tc(g: ParsedGame) -> str:
    if g.base_s is None:
        return g.time_class
    return f"{g.time_class} {int(g.base_s // 60)}+{int(g.inc_s)}" if g.base_s >= 60 else f"{g.time_class} {int(g.base_s)}s+{int(g.inc_s)}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0f}%"


def moment_block(m: CriticalMoment, game: ParsedGame) -> str:
    side = "White" if m.color == "white" else "Black"
    lines = [
        f"[[{m.ref}]] {m.classification.upper()} | {side} | {m.phase} | clock after move {_clock(m.clock)}"
        + (f", spent {m.time_spent:.1f}s" if m.time_spent is not None else ""),
        f"  position before the move (FEN): {m.fen_before}",
        f"  eval before: {m.eval_before} ({_pct(m.win_before)} win chance) -> after {m.played_san}: {m.eval_after} "
        f"({_pct(m.win_after)}); cost {m.win_drop:.0f} points of win chance",
    ]
    opts = [f"{i}) {l.san[0]} {l.eval_str} [line: {' '.join(l.san)}]" for i, l in enumerate(m.best_lines, 1) if l.san]
    if opts:
        lines.append("  engine's top choices: " + "; ".join(opts))
    if m.refutation and m.refutation.san:
        lines.append(f"  opponent's best reply after {m.played_san}: {' '.join(m.refutation.san)} (eval {m.refutation.eval_str})")
    if m.continuation:
        lines.append(f"  what actually happened next: {' '.join(m.continuation)}")
    if m.tags:
        lines.append("  verified patterns: " + ", ".join(f"{t} ({TAG_LABELS.get(t, t)})" for t in m.tags))
    for note in m.tag_notes:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def build_context(s: CoachSession) -> str:
    p = s.profile
    out: list[str] = []
    who = p["username"] + (f" ({p['title']} {p['name']})" if p.get("title") and p.get("name") else (f" ({p['name']})" if p.get("name") else ""))
    ratings = ", ".join(
        f"{k} {v['current']}" + (f" (best {v['best']})" if v.get("best") else "") for k, v in p["ratings"].items()
    ) or "unavailable"
    smp = p["sample"]
    out.append("## PLAYER")
    out.append(f"username: {who}")
    out.append(f"current Chess.com ratings: {ratings}")
    if p.get("tactics_highest"):
        out.append(f"puzzle (tactics) rating, highest: {p['tactics_highest']}")
    out.append(f"level: {smp['rating_band']} (average rating in these games: {smp['avg_rating']})")

    out.append("\n## SAMPLE")
    tcs = ", ".join(f"{k} x{v}" for k, v in smp["time_classes"].items())
    r = smp["record"]
    out.append(
        f"{smp['games']} most recent games ({smp['date_range'][0]} to {smp['date_range'][1]}): {tcs}. "
        f"Record {r['win']}W {r['loss']}L {r['draw']}D (score {smp['score']}%)."
    )
    for color in ("white", "black"):
        c = smp["by_color"][color]
        if c["games"]:
            out.append(f"as {color}: {c['games']} games, {c['win']}W {c['loss']}L {c['draw']}D")

    acc, err, tm = p["accuracy"], p["errors"], p["time"]
    out.append(f"\n## ENGINE SUMMARY ({s.engine_name}; all numbers below are exact, never recompute them)")
    out.append(
        f"engine accuracy (this app's metric, 0-100): overall {acc['overall']}; "
        + "; ".join(f"{k} {v}" for k, v in acc["by_phase"].items() if v is not None)
    )
    if acc.get("chesscom_avg") is not None:
        out.append(f"Chess.com's own Game Review accuracy for these games averaged {acc['chesscom_avg']} (different formula)")
    pg = err["per_game"]
    out.append(
        f"per game: {pg['inaccuracy']} inaccuracies, {pg['mistake']} mistakes, {pg['miss']} misses "
        f"(failed to punish the opponent's error), {pg['blunder']} blunders"
    )
    for phase, e in err["by_phase"].items():
        out.append(
            f"{phase}: {e['moves']} moves, {e['inaccuracies']} inaccuracies, {e['mistakes']} mistakes, "
            f"{e['misses']} misses, {e['blunders']} blunders"
        )
    out.append(
        f"serious errors (mistakes + misses + blunders) among critical moments: {tm['serious_errors']}; "
        f"made in time pressure: {tm['in_time_pressure']}; made in 2 seconds or less: {tm['played_quickly']}"
    )
    if tm["lost_on_time"]:
        out.append("lost on time: " + ", ".join(tm["lost_on_time"]))
    conv = p["conversion"]
    out.append("thrown wins (reached 80%+ win chance after move 10 but did not win): " + (", ".join(conv["thrown_wins"]) or "none"))
    out.append("comebacks (fell to 20% or less but did not lose): " + (", ".join(conv["comebacks"]) or "none"))
    if p["patterns"]:
        out.append("verified patterns across critical moments:")
        for pat in p["patterns"]:
            out.append(f"  {pat['tag']} ({pat['label']}): {pat['count']}x in " + ", ".join(f"[[{r}]]" for r in pat["refs"]))
    out.append("openings (player's side, opening family): ")
    for o in p["openings"]:
        out.append(
            f"  {o['color']} / {o['opening']}: {o['games']} game{'s' if o['games'] != 1 else ''}, {o['win']}W {o['loss']}L {o['draw']}D, "
            f"opening-phase accuracy {o['opening_accuracy']} ({', '.join(o['gids'])})"
        )

    out.append("\n## GAMES (G1 is the most recent)")
    for g in s.games:
        a = s.analyses[g.gid]
        crit = ", ".join(f"[[{m.ref}]]" for m in a.moments) or "none"
        out.append(
            f"{g.gid} | {g.date} | {_tc(g)} | {g.player_color} vs {g.opponent} ({g.opponent_rating}) | "
            f"{g.outcome.upper()} ({'opponent ' if g.outcome == 'win' else ''}{g.end_reason}) | {g.opening} | {len(g.plies)} plies | "
            f"engine accuracy {a.accuracy} vs opponent {a.opponent_accuracy} | "
            f"win chance range {a.low_win:.0f}%-{a.peak_win:.0f}% | critical moments: {crit}"
        )

    moments = sorted(s.moments(), key=lambda m: m.win_drop, reverse=True)
    shown = moments[:MAX_MOMENTS]
    out.append(f"\n## CRITICAL MOMENTS (the player's costliest moves, re-checked with a deeper search; {len(shown)} of {len(moments)})")
    for m in sorted(shown, key=lambda m: (int(m.gid[1:]), m.ply)):
        out.append(moment_block(m, s.game(m.gid)))
    if len(moments) > len(shown):
        out.append(f"({len(moments) - len(shown)} smaller moments omitted; use get_moment or get_game_moves to inspect them)")
    return "\n".join(out)


def build_baseline_context(s: CoachSession) -> str:
    """Ablation used by the eval harness: the same games as raw PGN movetext with no engine data."""
    out = [f"## PLAYER\nusername: {s.username}"]
    for g in s.games:
        moves = " ".join(p.label for p in g.plies)
        out.append(
            f"\n{g.gid} | {g.date} | {g.player_color} vs {g.opponent} ({g.opponent_rating}) | {g.outcome.upper()} ({g.end_reason}) | {g.opening}\n{moves}"
        )
    return "\n".join(out)
