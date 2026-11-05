import pytest

from coach.context import build_context
from coach.verify import Evidence, verify


def _v(session, text, evidence=None):
    return verify(text, session, build_context(session), evidence)


def test_valid_citation_and_engine_endorsed_recommendation(session, crit):
    r = _v(session, f"In [[{crit['ref']}]] you moved too fast. The engine preferred {crit['best']} instead.")
    assert r["citations"] == {"total": 1, "ok": 1, "failures": []}
    assert r["moves"]["ok"] == r["moves"]["total"] >= 1
    assert r["recommendations"]["ok"] == 1
    assert r["score"] == 1.0


def test_citation_with_wrong_move_is_flagged(session, crit):
    r = _v(session, f"Look at [[{crit['wrong_ref']}]].")
    assert r["citations"]["ok"] == 0
    assert f"the move played was {crit['label']}" in r["citations"]["failures"][0]


def test_citation_to_nonexistent_game_or_move_is_flagged(session):
    r = _v(session, "See [[G9 12.Nf3]] and [[G1 99...Qc7]].")
    assert r["citations"]["total"] == 2 and r["citations"]["ok"] == 0


def test_invented_move_is_flagged(session, crit):
    r = _v(session, f"In [[{crit['ref']}]] the answer {crit['invented']} wins on the spot.")
    assert crit["invented"] in r["moves"]["failures"]
    assert r["score"] < 1.0


def test_recommending_a_move_from_nowhere_is_flagged(session, crit):
    # legal somewhere near the moment, but not at the decision point and not in any engine line
    if not crit["nowhere"]:
        pytest.skip("no suitable move in this engine's lines")
    r = _v(session, f"In [[{crit['ref']}]] you should have played {crit['nowhere']} with a crushing attack.")
    assert crit["nowhere"] in r["flagged"]


def test_bad_recommendation_is_flagged_even_if_legal(session, crit):
    bad = crit["outsider"]  # legal at the decision point, not among the engine's top choices
    r = _v(session, f"In [[{crit['ref']}]] the best move was {bad}.")
    assert r["moves"]["failures"] == []
    assert r["recommendations"]["failures"] == [bad]


def test_tool_checked_recommendation_is_accepted(session, crit):
    bad = crit["outsider"]
    ev = Evidence(endorsed={(crit["moment"].fen_before, bad)})
    r = _v(session, f"In [[{crit['ref']}]] the best move was {bad}.", ev)
    assert r["recommendations"]["failures"] == []


def test_made_up_evaluation_is_flagged(session, crit):
    r = _v(session, f"After [[{crit['ref']}]] you were at {crit['fake_eval']}.")
    assert r["evals"]["failures"] == [crit["fake_eval"]]


def test_squares_are_not_mistaken_for_moves(session, crit):
    r = _v(session, f"Your knight on e4 and the pawn on d5 were weak in [[{crit['ref']}]].")
    assert r["moves"]["total"] == 0


def test_numbered_pawn_moves_are_checked(session):
    # any played move is citable; these are the real game continuation (engine-independent)
    ok = _v(session, "The game went 23.Ne3 Rcd8 after [[G1 22...Qc7]].")
    assert ok["moves"]["failures"] == []
    bad = _v(session, "After [[G1 22...Qc7]] White played 23.h8=Q.")
    assert bad["moves"]["failures"]
