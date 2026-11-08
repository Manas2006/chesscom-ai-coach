"""Freeze a coach report for a fixture player into fixtures/<player>/report.json.

The sample review on the landing page (and the "browse while you wait" panel) loads that file
instead of calling the LLM, so it is instant and works with no API key. The file records the
analysis version and the exact game uuids it was written for; the service ignores it when either
no longer matches, and falls back to generating a fresh report.

    python -m coach.snapshot manas             # analyse the fixture locally and ask the LLM (needs a key)
    python -m coach.snapshot manas --from-url https://host/api/sessions/<sid>   # copy a live session's report
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

from .chesscom import normalize_username
from .config import Settings
from .engine import ANALYSIS_VERSION


def _from_live(url: str) -> tuple[dict, list[str], str, int]:
    view = httpx.get(url, timeout=60).raise_for_status().json()
    if not view.get("report"):
        raise SystemExit("that session has no report yet: open it in the browser and let the coach finish first")
    return view["report"], [g["uuid"] for g in view["games"]], view.get("engine") or "", int(view.get("analysis_version", ANALYSIS_VERSION))


def _from_local(settings: Settings, username: str, games: int, time_class: str, allow_template: bool) -> tuple[dict, list[str], str, int]:
    from .service import CoachService

    svc = CoachService(settings)
    try:
        if svc.provider is None and not allow_template:
            raise SystemExit("no LLM key configured: set ANTHROPIC_API_KEY or OPENAI_API_KEY, or pass --allow-template")
        job = svc.start(username, games, time_class, demo=True)
        while job.status not in ("done", "error"):
            time.sleep(0.5)
        if job.status == "error":
            raise SystemExit(f"analysis failed: {job.error}")
        session = svc.session(job.session_id)
        session.report = None  # never copy a report that was itself seeded from an older snapshot
        report = svc.report(job.session_id, refresh=True)
        return report, [g.uuid for g in session.games], svc.engine_name or "", ANALYSIS_VERSION
    finally:
        svc.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username", help="fixture player (a directory under fixtures/)")
    ap.add_argument("--games", type=int, default=30, help="how many fixture games the sample uses (default: all)")
    ap.add_argument("--time-class", default="all")
    ap.add_argument("--from-url", help="copy the report from a running server's /api/sessions/<sid> instead of generating")
    ap.add_argument("--allow-template", action="store_true", help="write the engine-only template report when no LLM key is set")
    ap.add_argument("--out", type=Path, help="destination (default: fixtures/<player>/report.json)")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    username = normalize_username(args.username)
    out = args.out or settings.fixtures_dir / username / "report.json"
    if not (settings.fixtures_dir / username / "games.json").exists():
        raise SystemExit(f"no fixture at {settings.fixtures_dir / username}")

    if args.from_url:
        report, uuids, engine, version = _from_live(args.from_url)
    else:
        report, uuids, engine, version = _from_local(settings, username, args.games, args.time_class, args.allow_template)

    snapshot = {
        "player": username,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": engine,
        "analysis_version": version,
        "game_uuids": uuids,
        "report": report,
    }
    out.write_text(json.dumps(snapshot, indent=2) + "\n")
    v = report.get("verification") or {}
    print(f"wrote {out} ({report.get('mode')}, {len(uuids)} games, grounding {v.get('score', 'n/a')}, {len((report.get('markdown') or '').split())} words)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
