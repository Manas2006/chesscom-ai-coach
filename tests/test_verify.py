from coach.context import build_context
from coach.verify import Evidence, verify


def _v(session, text, evidence=None):
    return verify(text, session, build_context(session), evidence)


def test_valid_citation_and_engine_endorsed_recommendation(session):
    r = _v(session, "In [[G1 22...Qc7]] you moved too fast. The engine preferred Nxe4, winning a pawn.")
    assert r["citations"] == {"total": 1, "ok": 1, "failures": []}
    assert r["moves"]["ok"] == r["moves"]["total"] >= 1
    assert r["recommendations"]["ok"] == 1
    assert r["score"] == 1.0


def test_citation_with_wrong_move_is_flagged(session):
    r = _v(session, "Look at [[G1 22...Qd7]].")
    assert r["citations"]["ok"] == 0
    assert "the move played was 22...Qc7" in r["citations"]["failures"][0]


def test_citation_to_nonexistent_game_or_move_is_flagged(session):
    r = _v(session, "See [[G9 12.Nf3]] and [[G1 99...Qc7]].")
    assert r["citations"]["total"] == 2 and r["citations"]["ok"] == 0


def test_invented_move_is_flagged(session):
    r = _v(session, "In [[G1 22...Qc7]] White could answer with Nxh8 and win.")
    assert "Nxh8" in r["moves"]["failures"]
    assert r["score"] < 1.0


def test_recommending_a_move_from_nowhere_is_flagged(session):
    # Qh5 is legal deep inside the engine line, but no engine line plays it: recommending it is unsupported.
    r = _v(session, "In [[G1 22...Qc7]] you should have played Qh5 with a crushing attack.")
    assert "Qh5" in r["flagged"]


def test_bad_recommendation_is_flagged_even_if_legal(session):
    # Kh8 is legal before 22...Qc7 but it is not among the engine's top choices
    r = _v(session, "In [[G1 22...Qc7]] the best move was Kh8.")
    assert r["moves"]["failures"] == []
    assert r["recommendations"]["failures"] == ["Kh8"]


def test_tool_checked_recommendation_is_accepted(session):
    g = session.games[0]
    ply = g.find_ply(22, "black")
    ev = Evidence(endorsed={(ply.fen_before, "Kh8")})
    r = _v(session, "In [[G1 22...Qc7]] the best move was Kh8.", ev)
    assert r["recommendations"]["failures"] == []


def test_made_up_evaluation_is_flagged(session):
    r = _v(session, "After [[G1 22...Qc7]] you were at +7.3.")
    assert r["evals"]["failures"] == ["+7.3"]


def test_squares_are_not_mistaken_for_moves(session):
    r = _v(session, "Your knight on e4 and the pawn on d5 were weak in [[G1 22...Qc7]].")
    assert r["moves"]["total"] == 0


def test_numbered_pawn_moves_are_checked(session):
    ok = _v(session, "The game went 23.Ne3 Rcd8 after [[G1 22...Qc7]].")
    assert ok["moves"]["failures"] == []
    bad = _v(session, "After [[G1 22...Qc7]] White played 23.h8=Q.")
    assert bad["moves"]["failures"]
