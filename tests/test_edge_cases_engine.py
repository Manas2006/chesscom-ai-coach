"""Edge cases that run Stockfish: crafted games (mate, stalemate, no moves), tool-argument tolerance,
board rendering bounds, practice input validation, analysis caching, chat history and HTTP validation."""

import json

import chess
import pytest
from fastapi.testclient import TestClient

from coach import agent
from coach.engine import GameAnalysis, analyse_game
from coach.llm import LLMResponse, ScriptedProvider, ToolCall
from coach.pgn import parse_game
from coach.profile import build_profile
from coach.server import create_app
from coach.service import _eval_label, rebind
from coach.session import CoachSession

HEADERS = '[Event "x"]\n[Site "Chess.com"]\n[White "Alice"]\n[Black "Bob"]\n[Result "*"]\n'


def _raw(movetext: str, **over) -> dict:
    raw = {
        "pgn": HEADERS + "\n" + movetext,
        "white": {"username": "Alice", "rating": 1500, "result": "win"},
        "black": {"username": "Bob", "rating": 1480, "result": "checkmated"},
        "time_class": "blitz",
        "time_control": "180",
        "rules": "chess",
        "rated": True,
        "end_time": 1_700_000_000,
        "url": "https://www.chess.com/game/live/99",
        "uuid": "crafted-1",
    }
    raw.update(over)
    return raw


def _analyse(service, settings, game):
    return analyse_game(service.pool, game, settings.engine_depth, settings.deep_nodes, settings.refutation_nodes)


# ---------------------------------------------------------------------------------------------
# Crafted games
# ---------------------------------------------------------------------------------------------
def test_scholars_mate_from_the_losing_side(service, settings):
    g = parse_game(_raw("1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0"), "bob", "G1")
    a = _analyse(service, settings, g)
    assert a.evals[-1].terminal == "checkmate" and a.evals[-1].win("black") == 0.0 and a.evals[-1].win("white") == 100.0
    assert _eval_label(a.evals[-1], "black") == "1-0" and _eval_label(a.evals[-1], "white") == "1-0"
    mate_move = a.moves[-1]
    assert mate_move.classification == "best" and mate_move.eval_after == "#", "delivering mate is always the best move"
    # Black's 3...Nf6 allowed mate in one: a blunder, tagged, with the mating reply as the refutation.
    nf6 = next(m for m in a.moments if m.played_san == "Nf6")
    # (a "miss" if the engine also calls 3.Qh5 an error: then Nf6 failed to punish it)
    assert nf6.classification in ("blunder", "miss") and nf6.win_after == 0.0 and nf6.eval_after == "#-1"
    assert "allows_mate" in nf6.tags and nf6.refutation and nf6.refutation.san[0] == "Qxf7#" and nf6.refutation.mate == -1
    assert nf6.continuation == ["4.Qxf7#"]
    assert a.counts["blunder"] + a.counts["miss"] >= 1 and a.low_win == 0.0
    # The round trip through JSON (the on-disk analysis cache) is lossless.
    assert GameAnalysis.from_dict(json.loads(json.dumps(a.to_dict()))).to_dict() == a.to_dict()


def test_stalemating_from_a_winning_position_is_a_blunder_without_a_refutation(service, settings):
    fen = "k7/2Q5/8/8/8/8/8/K7 w - - 0 1"
    pgn = HEADERS + f'[SetUp "1"]\n[FEN "{fen}"]\n\n1. Ka2 1/2-1/2'
    g = parse_game(_raw("", pgn=pgn, white={"username": "Alice", "result": "stalemate"}, black={"username": "Bob", "result": "stalemate"}), "alice", "G1")
    assert g.outcome == "draw" and g.end_reason == "stalemate"
    a = _analyse(service, settings, g)
    assert a.evals[1].terminal == "draw" and a.evals[1].win("white") == 50.0 and _eval_label(a.evals[1], "white") == "½-½"
    # How winning depends on the Stockfish build (some see the mate at depth 12, some score it +5 or so),
    # so the assertions are about the rules, not about one engine's number.
    assert a.moves[0].win_before > 75, "the engine knows the position before the blunder is clearly winning"
    (m,) = a.moments
    assert m.classification == "blunder" and m.refutation is None and m.win_after == 50.0 and m.win_drop >= 25
    assert "was_winning" in m.tags and m.phase == "endgame"
    assert ("missed_mate" in m.tags) == (m.best_lines[0].mate is not None), "tagged exactly when the deep search found the mate"
    assert a.moves[0].classification == "blunder" and a.accuracy is not None and a.accuracy < 50


def test_a_game_with_no_moves_analyses_to_an_empty_shell(service, settings):
    g = parse_game(_raw("*", uuid="crafted-empty"), "alice", "G1")
    a = _analyse(service, settings, g)
    assert len(a.evals) == 1 and a.moves == [] and a.moments == []
    assert a.accuracy is None and a.opponent_accuracy is None and a.peak_win == a.low_win == 50.0
    assert all(v == 0 for v in a.counts.values()) and all(p["accuracy"] is None for p in a.phase_stats.values())
    p = build_profile("alice", {}, {}, [g], {"G1": a})
    assert p["accuracy"]["overall"] is None and p["conversion"] == {"thrown_wins": [], "comebacks": []}


def test_opponent_moves_are_classified_from_their_own_side(session):
    for g in session.games:
        a = session.analyses[g.gid]
        for mv in a.moves:
            assert 0 <= mv.win_before <= 100 and 0 <= mv.win_after <= 100 and mv.win_drop >= 0
            assert mv.accuracy == 100.0 if mv.classification == "best" else True
            assert mv.phase in ("opening", "middlegame", "endgame")


# ---------------------------------------------------------------------------------------------
# Tool argument tolerance
# ---------------------------------------------------------------------------------------------
def test_tools_accept_sloppy_arguments_from_the_model(session, service, settings, crit):
    tb = agent.Toolbox(session, service.pool, settings)
    gid = crit["ref"].split(" ")[0]
    strict = tb.get_game_moves(gid, 5, 8)
    assert tb.get_game_moves(gid.lower(), "5", "8") == strict, "numbers as strings and lowercase ids are fine"
    assert tb.get_game_moves(gid, 8, 5) == strict, "a reversed range is swapped, not empty"
    assert tb.get_game_moves(gid, "junk", None).count("\n") >= 1, "unparseable from_move falls back to 1"
    assert "(no moves in that range)" in tb.get_game_moves(gid, 900)
    assert "No game 'G99'" in tb.get_game_moves("G99")
    assert "Bad arguments" in tb.call(ToolCall("a", "get_game_moves", {"game": gid}))
    assert "Bad arguments" in tb.call(ToolCall("b", "get_game_moves", "not-a-dict"))
    assert "does not match any move" in tb.call(ToolCall("c", "get_moment", {"ref": 12345}))
    assert "does not match any move" in tb.call(ToolCall("d", "analyze_position", {"ref": None}))
    assert "not a legal move" in tb.call(ToolCall("e", "evaluate_move", {"ref": crit["ref"], "move": "--"}))
    assert "not a legal move" in tb.call(ToolCall("f", "evaluate_move", {"ref": crit["ref"], "move": ""}))
    assert tb.evidence.texts, "every tool answer, even an error, is recorded as evidence text"


def test_evaluating_the_game_move_itself_says_so(session, service, settings, crit):
    tb = agent.Toolbox(session, service.pool, settings)
    out = tb.evaluate_move(crit["ref"], crit["played"])
    assert "the same as the move played" in out and "worse than the engine's best" in out
    assert (crit["moment"].fen_before, crit["played"]) not in tb.evidence.endorsed


def test_analyze_position_after_the_final_move_of_a_mated_game(service, settings):
    g = parse_game(_raw("1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0", uuid="crafted-2"), "bob", "G1")
    a = _analyse(service, settings, g)
    s = CoachSession(sid="t", username="bob", games=[g], analyses={"G1": a}, profile=build_profile("bob", {}, {}, [g], {"G1": a}))
    tb = agent.Toolbox(s, service.pool, settings)
    assert "game over (1-0)" in tb.analyze_position("G1 4.Qxf7#", when="after")
    before = tb.analyze_position("G1 4.Qxf7", when="before")
    assert "1) Qxf7#" in before and "White to move" in before
    moves = tb.get_game_moves("G1")
    assert "4.Qxf7# mate" in moves
    nf6_row = next(r for r in moves.splitlines() if r.startswith("3...Nf6"))
    assert ("blunder" in nf6_row or "miss" in nf6_row) and "(engine:" in nf6_row


# ---------------------------------------------------------------------------------------------
# Repair, history and reports
# ---------------------------------------------------------------------------------------------
def test_a_worse_repair_is_discarded(session, service, settings, crit):
    first = f"In [[{crit['ref']}]] the engine preferred {crit['best']}, but {crit['invented']} was also an option."
    worse = f"In [[{crit['wrong_ref']}]] you should have played {crit['invented']} or {crit['nowhere'] or crit['invented']}."
    provider = ScriptedProvider([LLMResponse(text=first), LLMResponse(text=worse)])
    out = agent.chat(session, provider, service.pool, settings, "Thoughts?")
    assert out["repaired"] is False and out["markdown"] == first
    assert out["verification_before_repair"] is None and out["verification"]["score"] < 1.0
    assert out["usage"]["rounds"] == 2, "the failed repair still counts towards usage"
    assert session.chat_history[-1]["text"] == first


def test_chat_history_is_capped_and_messages_are_trimmed(session, service, settings):
    session.chat_history.clear()
    provider = ScriptedProvider([LLMResponse(text="ok")] * 20)
    for i in range(9):
        agent.chat(session, provider, service.pool, settings, f"question {i}")
    assert len(session.chat_history) == 18
    sent = provider.calls[-1]["history"]
    assert len(sent) == 13 and sent[0]["role"] == "user" and sent[-1]["text"] == "question 8"
    long = "x" * 5000
    agent.chat(session, provider, service.pool, settings, long)
    assert len(provider.calls[-1]["history"][-1]["text"]) == 2000
    session.chat_history.clear()


def test_chat_without_a_provider_and_with_a_blank_message(session, service, settings):
    out = agent.chat(session, None, service.pool, settings, "   ")
    assert out["mode"] == "template" and "API_KEY" in out["markdown"] and out["verification"] is None


def test_template_report_without_any_moments(service, settings):
    g = parse_game(_raw("*", uuid="crafted-empty-2"), "alice", "G1")
    a = _analyse(service, settings, g)
    s = CoachSession(sid="t", username="alice", games=[g], analyses={"G1": a}, profile=build_profile("alice", {}, {}, [g], {"G1": a}))
    rep = agent.generate_report(s, None, service.pool, settings)
    md = rep["markdown"]
    assert "No recurring tactical pattern" in md and "## Your plan for the next 7 days" in md
    assert "Review each moment above" in md and rep["verification"]["score"] == 1.0


def test_report_falls_back_to_the_template_when_the_llm_fails(service, session):
    from coach.llm import LLMError

    class Broken:
        name, model = "broken", "broken"

        def complete(self, *a, **k):
            raise LLMError("boom")

        def stream(self, *a, **k):
            raise LLMError("boom")

    old_provider, old_report = service.provider, session.report
    service.provider, session.report = Broken(), None
    try:
        rep = service.report(session.sid)
        assert rep["mode"] == "template" and "boom" in rep["error"]
        assert service.report(session.sid) is rep, "the fallback is cached like any report"
        assert "language model call failed" in service.chat(session.sid, "hi")["markdown"]
    finally:
        service.provider, session.report = old_provider, old_report


# ---------------------------------------------------------------------------------------------
# Analysis cache
# ---------------------------------------------------------------------------------------------
def test_cached_analyses_are_rebound_to_the_sessions_ids_and_survive_corruption(service, settings):
    g = parse_game(_raw("1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0", uuid="crafted-cache"), "bob", "G1")
    first = service._analyse_one(g)
    path = service._cache_path(g)
    assert path.exists()
    g2 = parse_game(_raw("1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0", uuid="crafted-cache"), "bob", "G5")
    second = service._analyse_one(g2)
    assert second.gid == "G5" and all(m.gid == "G5" and m.ref.startswith("G5 ") for m in second.moments)
    assert [m.ply for m in second.moments] == [m.ply for m in first.moments]
    path.write_text("{corrupt")
    third = service._analyse_one(g2)
    assert third.to_dict() == second.to_dict() and json.loads(path.read_text())["gid"] == "G5"
    # a different colour of the same game is a different cache entry
    g3 = parse_game(_raw("1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0", uuid="crafted-cache"), "alice", "G1")
    assert service._cache_path(g3) != path


def test_rebind_relabels_refs_only():
    a = GameAnalysis("G1", 12, [], [], [], None, None, {}, {}, 50.0, 50.0)
    assert rebind(a, "G9").gid == "G9"


# ---------------------------------------------------------------------------------------------
# Boards and practice
# ---------------------------------------------------------------------------------------------
def test_board_rendering_clamps_plies_and_ignores_bad_moves(service, session, crit):
    gid = crit["ref"].split(" ")[0]
    g = session.game(gid)
    start = service.board_svg(session.sid, gid, -50)
    assert start == service.board_svg(session.sid, gid, -1) and start.startswith("<svg")
    assert service.board_svg(session.sid, gid, 10_000) == service.board_svg(session.sid, gid, len(g.plies) - 1)
    ply = crit["moment"].ply
    plain = service.board_svg(session.sid, gid, ply, mode="puzzle")
    for bad in ("e2e2", "a1a1q", "zz", "h8h8"):
        assert service.board_svg(session.sid, gid, ply, mode="puzzle", attempt=bad, reply=bad, reveal=bad) == plain
    with_attempt = service.board_svg(session.sid, gid, ply, mode="puzzle", attempt=crit["moment"].best_lines[0].uci[0])
    assert with_attempt != plain
    assert service.board_svg(session.sid, gid, ply, mode="puzzle", mark="z9") == plain
    with pytest.raises(KeyError):
        service.board_svg(session.sid, "G77", 0)
    assert service.board_svg(session.sid, gid.lower(), 0) == service.board_svg(session.sid, gid, 0)


def test_practice_rejects_malformed_and_null_moves(service, session, crit):
    for bad in ("0000", "zzzz", "e2e2", "", "a1a1q"):
        assert service.puzzle_attempt(session.sid, crit["ref"], bad) == {"legal": False}
    with pytest.raises(KeyError):
        service.puzzle_attempt(session.sid, "G1 1.e4", "e2e4")
    with pytest.raises(KeyError):
        service.puzzle_hint(session.sid, "nonsense", 1)
    assert service.puzzle_hint(session.sid, crit["ref"], 0)["level"] == 1
    assert service.puzzle_hint(session.sid, crit["ref"], 99)["level"] == 3


def test_practice_same_move_and_best_move_verdicts(service, session, crit):
    m = crit["moment"]
    same = service.puzzle_attempt(session.sid, crit["ref"], m.played_uci)
    assert same["verdict"] == "same" and same["solved"] is False and same["best_san"] is None
    best = service.puzzle_attempt(session.sid, crit["ref"], m.best_lines[0].uci[0])
    assert best["verdict"] == "best" and best["solved"] is True and best["drop"] == 0.0 and best["best_san"] == m.best_san
    puzzles = service.puzzles(session.sid)
    assert all(set(p["legal"]) == {mv.uci() for mv in chess.Board(p["fen"]).legal_moves} for p in puzzles)


def test_session_view_is_json_serialisable_and_consistent(service, session):
    view = service.session_view(session.sid)
    json.dumps(view)
    for g in view["games"]:
        assert len(g["win_curve"]) == len(g["eval_curve"]) == len(g["plies"]) + 1
        for p in g["plies"]:
            assert p["cls"] in ("best", "excellent", "good", "inaccuracy", "mistake", "miss", "blunder")
    refs = {m["ref"] for m in view["moments"]}
    assert all(r in refs for g in view["games"] for r in g["moments"])


# ---------------------------------------------------------------------------------------------
# HTTP validation
# ---------------------------------------------------------------------------------------------
def test_http_input_validation(service, session, crit):
    client = TestClient(create_app(service))
    sid = session.sid
    assert client.post("/api/analyze", json={"username": "gothamchess", "games": 31}).status_code == 422
    assert client.post("/api/analyze", json={"username": "gothamchess", "games": 0}).status_code == 422
    assert client.post("/api/analyze", json={"username": "gothamchess", "time_class": "bulet"}).status_code == 422
    assert client.post("/api/analyze", json={"username": "bad name!"}).status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get(f"/api/sessions/{sid}/board.svg", params={"game": "G1", "size": 50}).status_code == 422
    assert client.get(f"/api/sessions/{sid}/board.svg", params={"game": "G1", "mode": "sideways"}).status_code == 422
    assert client.get(f"/api/sessions/{sid}/board.svg", params={"game": "G1", "attempt": "e9e9"}).status_code == 422
    assert client.get(f"/api/sessions/{sid}/board.svg", params={"game": "G1", "ply": 5, "mode": "puzzle", "attempt": "e2e2"}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/board.svg", params={"game": "G77"}).status_code == 404
    assert client.get(f"/api/sessions/{sid}/board.svg", params={"game": "G1", "ply": -5}).status_code == 200
    assert client.post(f"/api/sessions/{sid}/puzzles/attempt", json={"ref": crit["ref"], "uci": "xx"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/puzzles/attempt", json={"ref": "G1 1.e4", "uci": "e2e4"}).status_code == 404
    assert client.post(f"/api/sessions/{sid}/puzzles/hint", json={"ref": crit["ref"], "level": 4}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/chat", json={"message": ""}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/chat", json={"message": "x" * 2001}).status_code == 422
    assert client.post("/api/sessions/expired/chat", json={"message": "hi"}).status_code == 404
    assert client.post("/api/sessions/expired/report/stream").status_code == 404
    assert client.get("/api/sessions/expired/puzzles").status_code == 404


def test_unknown_demo_player_fails_the_job_cleanly(service):
    client = TestClient(create_app(service))
    job_id = client.post("/api/analyze", json={"username": "nobody-here", "demo": True}).json()["job_id"]
    import time

    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert j["status"] == "error" and "No offline fixture" in j["error"] and j["session_id"] is None


# ---------------------------------------------------------------------------------------------
# Streaming details
# ---------------------------------------------------------------------------------------------
def test_repair_pass_is_narrated_to_the_stream(session, service, settings, crit):
    provider = ScriptedProvider(
        [
            LLMResponse(text=f"In [[{crit['wrong_ref']}]] you should have played {crit['invented']}."),
            LLMResponse(text=f"In [[{crit['ref']}]] the engine preferred {crit['best']}."),
        ]
    )
    events = []
    out = agent.chat(session, provider, service.pool, settings, "What went wrong?", on_event=events.append)
    kinds = [e["type"] for e in events]
    assert out["repaired"] is True and kinds.count("round") == 2, "the UI clears its draft when the repaired answer starts"
    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert any("did not check out" in s and "revising" in s for s in statuses)
    assert "".join(e["text"] for e in events[len(kinds) - kinds[::-1].index("round") :] if e["type"] == "delta") == out["markdown"]


def test_sse_relays_failures_as_an_error_event():
    from coach.service import CoachService

    def work(emit):
        emit({"type": "status", "text": "working"})
        raise RuntimeError("engine exploded")

    frames = list(CoachService.sse(work))
    events = [json.loads(f[5:].strip()) for f in frames]
    assert events[0] == {"type": "status", "text": "working"}
    assert events[-1] == {"type": "error", "message": "engine exploded"} and len(events) == 2
    assert all(f.endswith("\n\n") for f in frames)


def test_sse_stream_endpoint_surfaces_provider_failures(service, session):
    from coach.llm import LLMError

    class Broken:
        name, model = "broken", "broken"

        def complete(self, *a, **k):
            raise LLMError("quota exceeded")

        def stream(self, *a, **k):
            raise LLMError("quota exceeded")

    old = service.provider
    service.provider = Broken()
    try:
        client = TestClient(create_app(service))
        evs = [json.loads(l[5:]) for l in client.post(f"/api/sessions/{session.sid}/chat/stream", json={"message": "hi"}).text.splitlines() if l.startswith("data:")]
        assert evs[-1]["type"] == "done" and "quota exceeded" in evs[-1]["result"]["markdown"]
    finally:
        service.provider = old
