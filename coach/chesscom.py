"""Client for Chess.com's Published Data API (https://www.chess.com/news/view/published-data-api).

The API is read-only, unauthenticated and asks clients to send a descriptive User-Agent and to
avoid parallel requests. We request serially, cache on disk, and back off on HTTP 429.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Iterable

import httpx

API = "https://api.chess.com/pub"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")


class ChessComError(RuntimeError):
    pass


class PlayerNotFound(ChessComError):
    pass


def normalize_username(username: str) -> str:
    username = (username or "").strip().lstrip("@")
    if not USERNAME_RE.match(username):
        raise PlayerNotFound(f"'{username}' is not a valid Chess.com username")
    return username.lower()


def _filter_games(games: Iterable[dict], time_class: str) -> list[dict]:
    out = []
    for g in games:
        if g.get("rules") != "chess":  # skip chess960, bughouse, etc.
            continue
        if time_class != "all" and g.get("time_class") != time_class:
            continue
        if not g.get("pgn"):
            continue
        out.append(g)
    return out


class ChessComClient:
    def __init__(self, user_agent: str, cache_dir: Path, http: httpx.Client | None = None):
        self.http = http or httpx.Client(
            timeout=20.0, headers={"User-Agent": user_agent, "Accept": "application/json"}, follow_redirects=True
        )
        self.cache_dir = Path(cache_dir) / "http"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- low level -------------------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def _get(self, url: str, ttl_s: float | None) -> dict:
        path = self._cache_path(url)
        if path.exists():
            try:
                cached = json.loads(path.read_text())
                if ttl_s is None or time.time() - cached["t"] < ttl_s:
                    return cached["data"]
            except (ValueError, KeyError):
                pass
        delay = 1.0
        for attempt in range(4):
            try:
                resp = self.http.get(url)
            except httpx.HTTPError as exc:
                raise ChessComError(f"Could not reach Chess.com ({exc.__class__.__name__}). Try demo mode.") from exc
            if resp.status_code == 429 and attempt < 3:
                time.sleep(float(resp.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            if resp.status_code in (404, 410):
                raise PlayerNotFound("Chess.com has no player with that username")
            if resp.status_code != 200:
                raise ChessComError(f"Chess.com API returned HTTP {resp.status_code} for {url}")
            data = resp.json()
            path.write_text(json.dumps({"t": time.time(), "data": data}))
            return data
        raise ChessComError("Chess.com rate limit: please retry in a minute")

    # -- endpoints -------------------------------------------------------------------------
    def profile(self, username: str) -> dict:
        return self._get(f"{API}/player/{normalize_username(username)}", ttl_s=3600)

    def stats(self, username: str) -> dict:
        return self._get(f"{API}/player/{normalize_username(username)}/stats", ttl_s=3600)

    def archives(self, username: str) -> list[str]:
        data = self._get(f"{API}/player/{normalize_username(username)}/games/archives", ttl_s=600)
        return data.get("archives", [])

    def month(self, archive_url: str, is_current: bool) -> list[dict]:
        # Past months never change, so cache them forever; the current month changes as games finish.
        return self._get(archive_url, ttl_s=300 if is_current else None).get("games", [])

    def recent_games(self, username: str, n: int, time_class: str = "all", max_months: int = 12) -> list[dict]:
        archives = self.archives(username)
        if not archives:
            return []
        collected: list[dict] = []
        for i, url in enumerate(reversed(archives[-max_months:])):
            collected.extend(_filter_games(self.month(url, is_current=(i == 0)), time_class))
            if len(collected) >= n:
                break
        collected.sort(key=lambda g: g.get("end_time", 0), reverse=True)
        return collected[:n]


class FixtureClient:
    """Serves real Chess.com responses saved under fixtures/<username>/ (offline demo and tests)."""

    def __init__(self, fixtures_dir: Path):
        self.root = Path(fixtures_dir)

    def usernames(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "games.json").exists()) if self.root.exists() else []

    def _dir(self, username: str) -> Path:
        d = self.root / normalize_username(username)
        if not (d / "games.json").exists():
            raise PlayerNotFound(f"No offline fixture for '{username}'. Available: {', '.join(self.usernames())}")
        return d

    def profile(self, username: str) -> dict:
        return json.loads((self._dir(username) / "player.json").read_text())["profile"]

    def stats(self, username: str) -> dict:
        return json.loads((self._dir(username) / "player.json").read_text())["stats"]

    def recent_games(self, username: str, n: int, time_class: str = "all", max_months: int = 12) -> list[dict]:
        d = self._dir(username)
        meta = json.loads((d / "games.json").read_text())["games"]
        games = []
        for m in meta:
            g = dict(m)
            g["pgn"] = (d / "pgn" / f"{m['uuid']}.pgn").read_text()
            games.append(g)
        games = _filter_games(games, time_class)
        games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
        return games[:n]
