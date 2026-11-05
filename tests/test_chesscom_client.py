"""The live Chess.com client, exercised against a mocked API built from the real fixture responses."""

import json

import httpx
import pytest

from coach.chesscom import API, ChessComClient, PlayerNotFound
from coach.config import ROOT

FIX = ROOT / "fixtures" / "gothamchess"


def _fixture_games():
    meta = json.loads((FIX / "games.json").read_text())["games"]
    return [dict(m, pgn=(FIX / "pgn" / f"{m['uuid']}.pgn").read_text()) for m in meta]


def _client(tmp_path, handler):
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test-agent"})
    return ChessComClient("test-agent", tmp_path, http=http)


def test_walks_archives_newest_first_filters_variants_and_caches(tmp_path):
    games = _fixture_games()
    variant = dict(games[0], rules="chess960", uuid="variant", end_time=games[0]["end_time"] + 10)
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        assert req.headers["User-Agent"] == "test-agent"
        path = req.url.path
        if path.endswith("/games/archives"):
            return httpx.Response(200, json={"archives": [f"{API}/player/gothamchess/games/2026/09", f"{API}/player/gothamchess/games/2026/10"]})
        if path.endswith("/2026/10"):
            return httpx.Response(200, json={"games": games[:4] + [variant]})
        if path.endswith("/2026/09"):
            return httpx.Response(200, json={"games": games[4:]})
        return httpx.Response(404)

    c = _client(tmp_path, handler)
    got = c.recent_games("GothamChess", 3)
    assert len(got) == 3 and all(g["rules"] == "chess" for g in got)
    assert [g["end_time"] for g in got] == sorted((g["end_time"] for g in got), reverse=True)
    assert not any(u.endswith("/2026/09") for u in calls), "stops once enough games are collected"
    got6 = c.recent_games("gothamchess", 6)
    assert len(got6) == 6
    n = len(calls)
    c.recent_games("gothamchess", 6)
    assert len(calls) == n, "second call is served from the disk cache"


def test_retries_on_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"username": "gothamchess"})

    assert _client(tmp_path, handler).profile("gothamchess")["username"] == "gothamchess"
    assert state["n"] == 2


def test_unknown_player(tmp_path):
    with pytest.raises(PlayerNotFound):
        _client(tmp_path, lambda req: httpx.Response(404, json={"code": 0})).archives("nobody-here")
