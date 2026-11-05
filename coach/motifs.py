"""Deterministic pattern tags for critical moments.

These are cheap, explainable heuristics computed from the board and the engine lines. They give
the LLM concrete, verified handles ("hangs_piece", "allows_fork", "time_pressure") instead of
asking it to read tactics off a FEN, which LLMs do unreliably.
"""

from __future__ import annotations

import chess

from .engine import PIECE_NAMES, PIECE_VALUES, CriticalMoment, EngineLine, material
from .pgn import ParsedGame

TAG_LABELS = {
    "hangs_piece": "left a piece undefended",
    "loses_material": "lost material to a forcing line",
    "allows_fork": "allowed a fork",
    "allows_mate": "allowed a mating attack",
    "missed_mate": "missed a forced mate",
    "missed_material_gain": "missed a chance to win material",
    "best_was_capture": "missed a capture",
    "best_was_check": "missed a forcing check",
    "time_pressure": "low on the clock",
    "played_quickly": "moved quickly (2s or less)",
    "long_think": "after a long think",
    "was_winning": "slipped while clearly winning",
}


def _even_prefix(line: EngineLine | None, max_plies: int = 4) -> list[str]:
    if not line:
        return []
    n = min(len(line.uci), max_plies)
    return line.uci[: n - (n % 2)] if n >= 2 else line.uci[:n]


def _diff(board: chess.Board, color: chess.Color) -> int:
    return material(board, color) - material(board, not color)


def _play(board: chess.Board, ucis: list[str]) -> chess.Board:
    b = board.copy()
    for u in ucis:
        mv = chess.Move.from_uci(u)
        if not b.is_legal(mv):
            break
        b.push(mv)
    return b


def _fmt_secs(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}" if s >= 60 else f"{s:.0f}s"


def tag_moment(m: CriticalMoment, game: ParsedGame, before: chess.Board, after: chess.Board, opp: str) -> None:
    me = chess.WHITE if m.color == "white" else chess.BLACK
    tags: list[str] = []
    notes: list[str] = []
    baseline = _diff(before, me)
    ref = m.refutation

    # --- what the played move allowed ------------------------------------------------------
    if ref and ref.mate is not None and ref.mate < 0:
        tags.append("allows_mate")
        notes.append(f"{m.played_san} allows a forced mate in {abs(ref.mate)} ({' '.join(ref.san[:4])})")

    if ref and ref.uci:
        first = chess.Move.from_uci(ref.uci[0])
        victim = after.piece_at(first.to_square)
        if victim and victim.color == me and PIECE_VALUES[victim.piece_type] >= 3:
            if not after.is_attacked_by(me, first.to_square):
                tags.append("hangs_piece")
                notes.append(
                    f"after {m.played_san} the {PIECE_NAMES[victim.piece_type]} on {chess.square_name(first.to_square)} "
                    f"is undefended and {ref.san[0]} wins it"
                )
        b1 = after.copy()
        if b1.is_legal(first):
            b1.push(first)
            targets = []
            for sq in b1.attacks(first.to_square):
                p = b1.piece_at(sq)
                if p and p.color == me and (p.piece_type == chess.KING or PIECE_VALUES[p.piece_type] >= 3):
                    targets.append(f"{PIECE_NAMES[p.piece_type]} on {chess.square_name(sq)}")
            if len(targets) >= 2:
                tags.append("allows_fork")
                notes.append(f"{ref.san[0]} attacks the " + " and the ".join(targets))

        end = _play(after, _even_prefix(ref))
        delta = _diff(end, me) - baseline
        if delta <= -2 and "hangs_piece" not in tags:
            tags.append("loses_material")
            shown = ' '.join(ref.san[: len(_even_prefix(ref))])
            notes.append(f"after {m.played_san}, the line {shown} wins about {abs(delta)} points of material")

    # --- what the player missed ---------------------------------------------------------------
    best = m.best_lines[0] if m.best_lines else None
    if best and best.mate is not None and best.mate > 0 and not m.eval_after.startswith("#"):
        tags.append("missed_mate")
        notes.append(f"{best.san[0]} started a forced mate in {best.mate} ({' '.join(best.san[:5])})")
    if best and best.uci:
        gain = _diff(_play(before, _even_prefix(best)), me) - baseline
        if gain >= 2:
            tags.append("missed_material_gain")
            shown = best.san[: len(_even_prefix(best))]
            verb = "promotes and nets" if any("=" in x for x in shown) else "wins"
            notes.append(f"{' '.join(shown)} {verb} about {gain} points of material")
        mv = chess.Move.from_uci(best.uci[0])
        if before.is_legal(mv):
            if before.is_capture(mv) and not before.is_capture(chess.Move.from_uci(m.played_uci)):
                tags.append("best_was_capture")
            if before.gives_check(mv) and not before.gives_check(chess.Move.from_uci(m.played_uci)):
                tags.append("best_was_check")

    # --- clock and context --------------------------------------------------------------------
    if game.is_live and m.clock is not None:
        base = game.base_s or 180
        if m.clock < max(15.0, 0.10 * base):
            tags.append("time_pressure")
            notes.append(f"only {_fmt_secs(m.clock)} left on the clock after this move")
        elif m.time_spent is not None and m.time_spent <= 2.0:
            tags.append("played_quickly")
            notes.append(f"played in {m.time_spent:.1f}s with {_fmt_secs(m.clock)} still on the clock")
        if m.time_spent is not None and m.time_spent >= max(20.0, 0.12 * base):
            tags.append("long_think")
            notes.append(f"spent {_fmt_secs(m.time_spent)} on this move")
    if m.win_before >= 75:
        tags.append("was_winning")

    m.tags, m.tag_notes = tags, notes
