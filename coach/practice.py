"""'Retry your mistakes': every critical moment becomes a puzzle graded by Stockfish.

A try counts as solved when it is the engine's best move or within 5 points of win chance of it
(the same bar the verifier uses for "as good as best"), so strong alternatives are accepted too.
"""

from __future__ import annotations

import chess

from .engine import PIECE_NAMES, EnginePool, analyse_lines, classify
from .session import CoachSession

SOLVED_WITHIN = 5.0

THEME_HINTS = [
    ("missed_mate", "There is a forced mate. Look at every check."),
    ("missed_punish", "Your opponent just made a mistake. Find the move that punishes it."),
    ("missed_material_gain", "You can win material here."),
    ("best_was_capture", "A capture is strong here. Calculate the trades."),
    ("best_was_check", "Start with a check."),
    ("allows_mate", "Your king is in danger. Find the move that keeps it safe."),
    ("allows_fork", "Watch out for a fork. Keep your valuable pieces out of reach."),
    ("hangs_piece", "Something can be left undefended. Find a move that keeps every piece protected."),
    ("loses_material", "Look at the opponent's forcing replies before you move."),
]


def puzzle_list(s: CoachSession) -> list[dict]:
    out = []
    for m in sorted(s.moments(), key=lambda x: x.win_drop, reverse=True):
        board = chess.Board(m.fen_before)
        out.append(
            {
                "ref": m.ref,
                "gid": m.gid,
                "ply": m.ply,
                "fen": m.fen_before,
                "to_move": m.color,
                "played_san": m.played_san,
                "classification": m.classification,
                "win_drop": m.win_drop,
                "legal": [mv.uci() for mv in board.legal_moves],
            }
        )
    return out


def _moment(s: CoachSession, ref: str):
    t = s.resolve_ref(ref)
    m = s.moment_at(t.game.gid, t.ply.index) if t else None
    if not m or not m.best_lines:
        raise KeyError(ref)
    return t, m


def attempt(s: CoachSession, pool: EnginePool, ref: str, uci: str, nodes: int) -> dict:
    t, m = _moment(s, ref)
    board = chess.Board(m.fen_before)
    try:
        mv = chess.Move.from_uci(uci)
    except ValueError:
        return {"legal": False}
    piece = board.piece_at(mv.from_square)
    if piece and piece.piece_type == chess.PAWN and chess.square_rank(mv.to_square) in (0, 7) and not mv.promotion:
        mv = chess.Move(mv.from_square, mv.to_square, promotion=chess.QUEEN)
    if not board.is_legal(mv):
        return {"legal": False}
    san = board.san(mv)
    best = m.best_lines[0]
    line = next((l for l in m.best_lines if l.uci and l.uci[0] == mv.uci()), None)
    reply_uci, reply_san, continuation = None, [], []
    after = board.copy()
    after.push(mv)
    if after.is_checkmate():
        win, eval_str = 100.0, "#"
    elif line is not None:
        win, eval_str, continuation = line.win, line.eval_str, line.san[1:]
    else:
        with pool.engine() as eng:
            replies = analyse_lines(eng, after, m.color, nodes, multipv=1, game=t.game.uuid)
        r = replies[0] if replies else None
        win, eval_str = (r.win, r.eval_str) if r else (50.0, "+0.0")
        if r and r.uci:
            reply_uci, reply_san = r.uci[0], r.san
    drop = round(max(0.0, best.win - win), 1)
    same = mv.uci() == m.played_uci
    solved = not same and drop < SOLVED_WITHIN
    if same:
        verdict, message = "same", f"That is the move from the game ({m.classification}). Look for something better."
    elif mv.uci() == best.uci[0]:
        verdict, message = "best", f"{san} is the engine's best move."
    elif solved:
        verdict, message = "excellent", f"{san} is excellent, practically as strong as the engine's {best.san[0]}."
    else:
        cls = classify(drop, False)
        verdict = cls
        message = f"{san} is {'an' if cls[0] in 'aeiou' else 'a'} {cls}" + (f": {reply_san[0]} answers it." if reply_san else ".")
    return {
        "legal": True,
        "uci": mv.uci(),
        "san": san,
        "verdict": verdict,
        "solved": solved,
        "drop": drop,
        "eval": eval_str,
        "message": message,
        "reply_uci": reply_uci,
        "reply_line": reply_san[:4],
        "best_san": best.san[0] if solved else None,
        "line": (continuation or best.san[1:])[:5] if solved else [],
    }


def hint(s: CoachSession, ref: str, level: int) -> dict:
    _, m = _moment(s, ref)
    best = m.best_lines[0]
    board = chess.Board(m.fen_before)
    mv = chess.Move.from_uci(best.uci[0])
    if level <= 1:
        text = next((h for tag, h in THEME_HINTS if tag in m.tags), "Check every forcing move for both sides: checks, captures and threats.")
        return {"level": 1, "text": text}
    if level == 2:
        p = board.piece_at(mv.from_square)
        return {
            "level": 2,
            "text": f"Move your {PIECE_NAMES[p.piece_type]} on {chess.square_name(mv.from_square)}.",
            "square": chess.square_name(mv.from_square),
        }
    return {
        "level": 3,
        "text": f"The engine plays {best.san[0]} ({best.eval_str}). Line: {' '.join(best.san)}.",
        "best_uci": best.uci[0],
        "best_san": best.san[0],
    }
