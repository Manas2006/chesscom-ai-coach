"""Deterministic motif tags, checked on hand-built positions so every rule is exercised exactly once."""

import chess

from coach.engine import CriticalMoment, EngineLine, PosEval, _terminal_eval, game_phase
from coach.motifs import TAG_LABELS, tag_moment
from coach.pgn import parse_game

HEADERS = '[Event "x"]\n[White "Alice"]\n[Black "Bob"]\n[Result "*"]\n'


def _game(time_class="blitz", time_control="180"):
    raw = {
        "pgn": HEADERS + "\n1. e4 e5 *",
        "white": {"username": "Alice", "result": "win"},
        "black": {"username": "Bob", "result": "resigned"},
        "time_class": time_class,
        "time_control": time_control,
    }
    return parse_game(raw, "alice", "G1")


def _line(before: chess.Board, sans: list[str], cp=None, mate=None, perspective="white") -> EngineLine:
    b, ucis = before.copy(), []
    for s in sans:
        mv = b.parse_san(s)
        ucis.append(mv.uci())
        b.push(mv)
    return EngineLine(san=sans, uci=ucis, cp=cp, mate=mate, perspective=perspective)


def _moment(before_fen: str, played: str, best: EngineLine | None, refutation: EngineLine | None, **over) -> tuple[CriticalMoment, chess.Board, chess.Board]:
    before = chess.Board(before_fen)
    mv = before.parse_san(played)
    after = before.copy()
    after.push(mv)
    fields = dict(
        ref=f"G1 1.{played}",
        gid="G1",
        ply=0,
        move_number=1,
        color="white" if before.turn == chess.WHITE else "black",
        played_san=played,
        played_uci=mv.uci(),
        fen_before=before.fen(),
        fen_after=after.fen(),
        classification="blunder",
        win_before=60.0,
        win_after=20.0,
        win_drop=40.0,
        eval_before="+0.5",
        eval_after="-3.0",
        phase="middlegame",
        clock=100.0,
        time_spent=5.0,
        best_lines=[best] if best else [],
        refutation=refutation,
        continuation=[],
    )
    fields.update(over)
    return CriticalMoment(**fields), before, after


def test_hanging_a_piece_is_named_with_square_and_capture():
    # White queen steps to d4 where the c6 knight simply takes it.
    before = chess.Board("6k1/8/2n5/8/8/8/8/3Q2K1 w - - 0 1")
    refutation = _line(chess.Board("6k1/8/2n5/8/3Q4/8/8/6K1 b - - 0 1"), ["Nxd4"], cp=-900)
    m, b, a = _moment(before.fen(), "Qd4", _line(before, ["Qd2", "Kf8"], cp=50), refutation)
    tag_moment(m, _game(), b, a, "black")
    assert m.tags == ["hangs_piece"], "a hanging piece is reported once, not also as 'loses material'"
    assert m.tag_notes == ["after Qd4 the queen on d4 is undefended and Nxd4 wins it"]


def test_knight_fork_and_material_loss_are_both_detected():
    # After the played move Black's knight hops to f3 with check, hitting the king on g1 and the queen on e1.
    before = chess.Board("6k1/8/8/4n3/8/8/8/3Q2K1 w - - 0 1")
    after = chess.Board("6k1/8/8/4n3/8/8/8/4Q1K1 b - - 0 1")
    ref = _line(after, ["Nf3+", "Kf1", "Nxe1", "Kxe1"], cp=-600)
    m, b, a = _moment(before.fen(), "Qe1", _line(before, ["Qd5", "Kf8"], cp=80), ref)
    tag_moment(m, _game(), b, a, "black")
    assert "allows_fork" in m.tags and "loses_material" in m.tags and "hangs_piece" not in m.tags
    assert any("Nf3+ attacks the queen on e1 and the king on g1" in n for n in m.tag_notes)
    assert any("wins about 6 points of material" in n for n in m.tag_notes)


def test_allowing_mate_and_missing_material_and_captures():
    # Rook could take the queen on d5 (and that was a capture, not a check); instead a quiet rook move allowed mate.
    before = chess.Board("6k1/5ppp/8/3q4/8/8/8/3R2K1 w - - 0 1")
    best = _line(before, ["Rxd5", "Kf8"], cp=900)
    after = chess.Board("6k1/5ppp/8/3q4/8/8/3R4/6K1 b - - 0 1")
    ref = _line(after, ["Qxd2", "Kf1", "Qd1#"], mate=-2)
    m, b, a = _moment(before.fen(), "Rd2", best, ref)
    tag_moment(m, _game(), b, a, "black")
    assert m.tags[0] == "allows_mate" and "Rd2 allows a forced mate in 2 (Qxd2 Kf1 Qd1#)" in m.tag_notes[0]
    assert "hangs_piece" in m.tags, "the rook on d2 is left to be taken"
    assert "missed_material_gain" in m.tags and any("Rxd5 Kf8 wins about 9 points of material" in n for n in m.tag_notes)
    assert "best_was_capture" in m.tags and "best_was_check" not in m.tags


def test_missing_a_mate_and_a_check():
    before = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    best = _line(before, ["Ra8#"], mate=1)
    m, b, a = _moment(before.fen(), "Rb1", best, _line(chess.Board("6k1/5ppp/8/8/8/8/8/1R4K1 b - - 0 1"), ["Kf8"], cp=400))
    tag_moment(m, _game(), b, a, "black")
    assert "missed_mate" in m.tags and "best_was_check" in m.tags and "missed_material_gain" not in m.tags
    assert any("Ra8# started a forced mate in 1" in n for n in m.tag_notes)
    # ...but not when the played move itself also mates (eval after is already a mate score)
    m2, b2, a2 = _moment(before.fen(), "Rb1", best, None, eval_after="#3")
    tag_moment(m2, _game(), b2, a2, "black")
    assert "missed_mate" not in m2.tags


def test_promotion_wording_in_material_gain():
    before = chess.Board("8/4P3/8/8/8/8/8/k5K1 w - - 0 1")
    best = _line(before, ["e8=Q", "Kb2"], cp=900)
    m, b, a = _moment(before.fen(), "Kf2", best, None)
    tag_moment(m, _game(), b, a, "black")
    assert "missed_material_gain" in m.tags and any("e8=Q Kb2 promotes and nets about 8 points" in n for n in m.tag_notes)


def test_clock_tags_depend_on_the_time_control():
    before = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    base = dict(best=None, refutation=None)
    m, b, a = _moment(before.fen(), "Rb1", clock=12.0, time_spent=1.0, win_before=80.0, **base)
    tag_moment(m, _game(), b, a, "black")
    assert m.tags == ["time_pressure", "was_winning"], "time pressure wins over 'played quickly'; 80% before means it was winning"
    assert "only 12s left on the clock" in m.tag_notes[0]

    m, b, a = _moment(before.fen(), "Rb1", clock=100.0, time_spent=1.5, win_before=50.0, **base)
    tag_moment(m, _game(), b, a, "black")
    assert m.tags == ["played_quickly"] and "played in 1.5s with 1:40 still on the clock" in m.tag_notes[0]

    m, b, a = _moment(before.fen(), "Rb1", clock=100.0, time_spent=30.0, win_before=50.0, **base)
    tag_moment(m, _game(), b, a, "black")
    assert m.tags == ["long_think"] and "spent 30s on this move" in m.tag_notes[0]

    # In a 10-minute game 12 seconds is time pressure (10% of base = 60s) and 30 seconds is not a long think (12% = 72s).
    m, b, a = _moment(before.fen(), "Rb1", clock=59.0, time_spent=30.0, win_before=50.0, **base)
    tag_moment(m, _game("rapid", "600"), b, a, "black")
    assert m.tags == ["time_pressure"]

    # Daily games carry no clock tags at all, whatever the numbers say.
    m, b, a = _moment(before.fen(), "Rb1", clock=5.0, time_spent=0.5, win_before=50.0, **base)
    tag_moment(m, _game("daily", "1/86400"), b, a, "black")
    assert m.tags == []


def test_every_tag_has_a_label():
    assert set(TAG_LABELS) >= {
        "missed_punish", "hangs_piece", "loses_material", "allows_fork", "allows_mate", "missed_mate", "missed_material_gain",
        "best_was_capture", "best_was_check", "time_pressure", "played_quickly", "long_think", "was_winning",
    }


def test_terminal_positions_and_phases():
    mate = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    mate.push_san("Ra8#")
    e = _terminal_eval(mate)
    assert e.terminal == "checkmate" and e.cp == 10_000 and e.win("white") == 100.0 and e.win("black") == 0.0
    assert _terminal_eval(chess.Board("7k/8/8/8/8/8/6q1/7K w - - 0 1")) is None, "a check that is not mate is not terminal"
    assert _terminal_eval(chess.Board("k7/2Q5/8/8/8/8/8/1K6 b - - 0 1")).terminal == "draw"  # stalemate
    assert _terminal_eval(chess.Board("8/8/8/8/8/8/8/k5K1 w - - 0 1")).terminal == "draw"  # insufficient material
    assert _terminal_eval(chess.Board("8/8/8/8/8/8/8/k4RK1 w - - 100 80")).terminal == "draw"  # fifty-move rule claimable
    assert _terminal_eval(chess.Board()) is None
    assert PosEval(cp=0, mate=None, best_uci=None, best_san=None, terminal="draw").win("black") == 50.0

    assert game_phase(chess.Board()) == "opening"
    b = chess.Board()
    b.fullmove_number = 13
    assert game_phase(b) == "middlegame"
    assert game_phase(chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")) == "endgame"  # 20 points of non-pawn material
