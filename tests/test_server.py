import time

from fastapi.testclient import TestClient

from coach.server import create_app


def test_end_to_end_http_flow(service):
    client = TestClient(create_app(service))
    h = client.get("/api/health").json()
    assert h["engine"]["ok"] and "gothamchess" in h["demo_players"]

    assert client.post("/api/analyze", json={"username": "x"}).status_code == 422
    job_id = client.post("/api/analyze", json={"username": "gothamchess", "games": 6, "demo": True}).json()["job_id"]
    for _ in range(600):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert j["status"] == "done", j
    sid = j["session_id"]

    view = client.get(f"/api/sessions/{sid}").json()
    assert len(view["games"]) == 6 and view["moments"]
    g = view["games"][0]
    assert len(g["win_curve"]) == len(g["plies"]) + 1

    rep = client.post(f"/api/sessions/{sid}/report").json()
    assert rep["mode"] == "template" and "## The big picture" in rep["markdown"]

    m = view["moments"][0]
    svg = client.get(f"/api/sessions/{sid}/board.svg", params={"game": m["gid"], "ply": m["ply"], "mode": "decision"})
    assert svg.status_code == 200 and svg.text.startswith("<svg")

    chat = client.post(f"/api/sessions/{sid}/chat", json={"message": "hello"}).json()
    assert "API_KEY" in chat["markdown"]

    assert client.get("/api/sessions/nope").status_code == 404
    assert client.get("/").status_code == 200
