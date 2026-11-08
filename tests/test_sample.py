"""The sample review: default fixture selection, the committed report that makes it instant, and the
snapshot command that writes that report."""

import dataclasses
import json
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from coach import snapshot
from coach.chesscom import FixtureClient
from coach.engine import ANALYSIS_VERSION
from coach.server import create_app
from coach.service import CoachService


@pytest.fixture
def fixtures_copy(tmp_path, settings):
    """A private copy of the fixtures directory so tests can add report.json files freely."""
    dst = tmp_path / "fixtures"
    shutil.copytree(settings.fixtures_dir, dst)
    return dst


def _wait(svc, job):
    for _ in range(600):
        if job.status in ("done", "error"):
            break
        time.sleep(0.05)
    assert job.status == "done", job.error
    return svc.session(job.session_id)


def _set_default(fixture_dir, value):
    meta = json.loads((fixture_dir / "games.json").read_text())
    meta["default"] = value
    (fixture_dir / "games.json").write_text(json.dumps(meta))


def test_fixture_summary_and_default_selection(fixtures_copy):
    fc = FixtureClient(fixtures_copy)
    s = fc.summary("GothamChess")
    assert s["player"] == "gothamchess" and s["display_name"] == "GothamChess" and s["games"] == 6
    assert s["time_classes"] == ["blitz"] and s["has_report"] is False and s["default"] is False
    bob = fc.summary("bobthegreatest123")
    assert bob["default"] is True and bob["games"] == 10 and bob["time_classes"] == ["bullet", "rapid"] and bob["display_name"] == "BobtheGreatest123"
    assert fc.default_player() == "bobthegreatest123", "the fixture marked default wins over alphabetical order"

    for name in fc.usernames():
        _set_default(fixtures_copy / name, False)
    assert FixtureClient(fixtures_copy).default_player() == sorted(fc.usernames())[0], "nothing marked: the first one wins"
    other = fixtures_copy / "zz-other"
    shutil.copytree(fixtures_copy / "gothamchess", other)
    _set_default(other, True)
    assert FixtureClient(fixtures_copy).default_player() == "zz-other"
    assert fc.report("gothamchess") is None
    (fixtures_copy / "gothamchess" / "report.json").write_text("{not json")
    assert fc.report("gothamchess") is None, "a corrupt snapshot is ignored, not fatal"


def test_health_describes_the_sample_and_honours_demo_player(settings, fixtures_copy):
    other = fixtures_copy / "aaa-first"
    shutil.copytree(fixtures_copy / "gothamchess", other)
    svc = CoachService(dataclasses.replace(settings, fixtures_dir=fixtures_copy))
    try:
        assert svc.health()["sample"]["player"] == "bobthegreatest123", "the marked default, not the alphabetical first"
    finally:
        svc.close()
    svc = CoachService(dataclasses.replace(settings, fixtures_dir=fixtures_copy, demo_player="GothamChess"))
    try:
        h = svc.health()
        assert h["sample"]["player"] == "gothamchess" and h["sample"]["games"] == 6
        assert h["demo_players"] == ["aaa-first", "bobthegreatest123", "gothamchess"]
    finally:
        svc.close()
    svc = CoachService(dataclasses.replace(settings, fixtures_dir=fixtures_copy, demo_player="nobody"))
    try:
        assert svc.health()["sample"]["player"] == "bobthegreatest123", "an unknown DEMO_PLAYER falls back to the default"
    finally:
        svc.close()
    svc = CoachService(dataclasses.replace(settings, fixtures_dir=fixtures_copy / "empty"))
    try:
        assert svc.health()["sample"] is None and svc.health()["demo_players"] == []
    finally:
        svc.close()


def test_snapshot_writes_a_report_the_service_then_serves_instantly(settings, fixtures_copy, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("COACH_CACHE_DIR", str(settings.cache_dir))
    monkeypatch.setenv("ENGINE_DEPTH", str(settings.engine_depth))
    monkeypatch.setattr(snapshot.Settings, "from_env", classmethod(lambda cls: dataclasses.replace(settings, fixtures_dir=fixtures_copy)))

    with pytest.raises(SystemExit, match="no LLM key"):
        snapshot.main(["gothamchess"])
    with pytest.raises(SystemExit, match="no fixture"):
        snapshot.main(["nobody", "--allow-template"])

    assert snapshot.main(["GothamChess", "--allow-template"]) == 0
    path = fixtures_copy / "gothamchess" / "report.json"
    snap = json.loads(path.read_text())
    assert snap["player"] == "gothamchess" and snap["analysis_version"] == ANALYSIS_VERSION and len(snap["game_uuids"]) == 6
    assert snap["report"]["mode"] == "template" and snap["report"]["verification"]["score"] == 1.0 and snap["engine"].startswith("Stockfish")
    assert FixtureClient(fixtures_copy).summary("gothamchess")["has_report"] is True

    svc = CoachService(dataclasses.replace(settings, fixtures_dir=fixtures_copy))
    try:
        s = _wait(svc, svc.start("gothamchess", 6, "all", demo=True))
        assert s.report and s.report["pregenerated"] is True and s.report["markdown"] == snap["report"]["markdown"]
        assert svc.report(s.sid) is s.report, "served from the seed, no generation"
        view = svc.session_view(s.sid)
        assert view["report"]["pregenerated"] and view["analysis_version"] == ANALYSIS_VERSION and all("uuid" in g for g in view["games"])

        # A different game selection is not the snapshot's: no seeding.
        s3 = _wait(svc, svc.start("gothamchess", 3, "all", demo=True))
        assert s3.report is None
        # Nor is a snapshot from an older analysis version.
        snap["analysis_version"] = ANALYSIS_VERSION - 1
        path.write_text(json.dumps(snap))
        s6 = _wait(svc, svc.start("gothamchess", 6, "all", demo=True))
        assert s6.report is None
        # Live sessions never read snapshots, whatever the file says.
        assert "pregenerated" not in (svc.session_view(s3.sid)["report"] or {})
    finally:
        svc.close()


def test_snapshot_can_copy_a_live_sessions_report(settings, fixtures_copy, monkeypatch):
    monkeypatch.setattr(snapshot.Settings, "from_env", classmethod(lambda cls: dataclasses.replace(settings, fixtures_dir=fixtures_copy)))
    svc = CoachService(dataclasses.replace(settings, fixtures_dir=fixtures_copy))
    try:
        s = _wait(svc, svc.start("gothamchess", 6, "all", demo=True))
        svc.report(s.sid)  # template report (no provider in tests)
        client = TestClient(create_app(svc))

        class FakeResponse:
            def __init__(self, url):
                self._r = client.get(url.replace("http://testserver", ""))

            def raise_for_status(self):
                self._r.raise_for_status()
                return self

            def json(self):
                return self._r.json()

        monkeypatch.setattr(snapshot.httpx, "get", lambda url, timeout=60: FakeResponse(url))
        out = fixtures_copy / "gothamchess" / "report.json"
        assert snapshot.main(["gothamchess", "--from-url", f"http://testserver/api/sessions/{s.sid}"]) == 0
        snap = json.loads(out.read_text())
        assert snap["game_uuids"] == [g.uuid for g in s.games] and snap["report"]["markdown"] == s.report["markdown"]
        assert snap["engine"] == svc.engine_name and snap["analysis_version"] == ANALYSIS_VERSION

        # Without a report on the session the command refuses rather than freezing an empty file.
        s2 = _wait(svc, svc.start("gothamchess", 2, "all", demo=True))
        with pytest.raises(SystemExit, match="no report yet"):
            snapshot.main(["gothamchess", "--from-url", f"http://testserver/api/sessions/{s2.sid}"])
    finally:
        svc.close()


def test_sample_copy_in_the_page_comes_from_health(service):
    client = TestClient(create_app(service))
    h = client.get("/api/health").json()
    assert h["sample"]["player"] in h["demo_players"]
    html = client.get("/").text
    assert 'id="demo-line"' in html and 'id="wait-card"' in html and 'id="bgjob"' in html
    assert "GothamChess" not in html, "no player is hard-coded in the page; the server decides the sample"
