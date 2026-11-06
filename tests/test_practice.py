import chess
from fastapi.testclient import TestClient

from coach.server import create_app


def test_puzzles_cover_every_key_moment(service, session):
    ps = service.puzzles(session.sid)
    assert len(ps) == len(session.moments())
    assert ps[0]["win_drop"] >= ps[-1]["win_drop"], "costliest mistake first"
    for p in ps:
        assert p["to_move"] == session.game(p["gid"]).player_color
        board = chess.Board(p["fen"])
        assert sorted(p["legal"]) == sorted(m.uci() for m in board.legal_moves)


def test_attempts_are_graded_by_the_engine(service, session, crit):
    m = crit["moment"]
    best = service.puzzle_attempt(session.sid, m.ref, m.best_lines[0].uci[0])
    assert best["solved"] and best["verdict"] == "best" and best["best_san"] == m.best_san

    same = service.puzzle_attempt(session.sid, m.ref, m.played_uci)
    assert not same["solved"] and same["verdict"] == "same"

    board = chess.Board(m.fen_before)
    outsider = board.parse_san(crit["outsider"]).uci()
    other = service.puzzle_attempt(session.sid, m.ref, outsider)
    assert other["legal"] and other["drop"] >= 0
    assert other["solved"] == (other["drop"] < 5)

    assert service.puzzle_attempt(session.sid, m.ref, "a1a1")["legal"] is False


def test_hints_escalate_to_the_solution(service, session, crit):
    m = crit["moment"]
    h1, h2, h3 = (service.puzzle_hint(session.sid, m.ref, lvl) for lvl in (1, 2, 3))
    assert h1["level"] == 1 and h1["text"]
    assert h2["square"] == m.best_lines[0].uci[0][:2]
    assert h3["best_san"] == m.best_san and h3["best_uci"] == m.best_lines[0].uci[0]


def test_practice_http_endpoints(service, session, crit):
    client = TestClient(create_app(service))
    m = crit["moment"]
    ps = client.get(f"/api/sessions/{session.sid}/puzzles").json()
    assert ps and ps[0]["ref"]
    r = client.post(f"/api/sessions/{session.sid}/puzzles/attempt", json={"ref": m.ref, "uci": m.best_lines[0].uci[0]}).json()
    assert r["solved"]
    assert client.post(f"/api/sessions/{session.sid}/puzzles/hint", json={"ref": m.ref, "level": 2}).json()["level"] == 2
    assert client.post(f"/api/sessions/{session.sid}/puzzles/attempt", json={"ref": "G9 1.e4", "uci": "e2e4"}).status_code == 404
    svg = client.get(
        f"/api/sessions/{session.sid}/board.svg",
        params={"game": m.gid, "ply": m.ply, "mode": "puzzle", "attempt": m.played_uci, "mark": m.played_uci[:2]},
    )
    assert svg.status_code == 200 and svg.text.startswith("<svg")
    bad = client.get(f"/api/sessions/{session.sid}/board.svg", params={"game": m.gid, "ply": m.ply, "mode": "puzzle", "attempt": "zz"})
    assert bad.status_code == 422
