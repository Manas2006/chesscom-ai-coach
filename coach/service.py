"""Orchestration: fetch -> parse -> analyse (cached, parallel) -> profile -> session."""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import chess
import chess.svg

from . import agent, practice
from .chesscom import ChessComClient, ChessComError, FixtureClient, PlayerNotFound, normalize_username
from .config import Settings
from .engine import ANALYSIS_VERSION, EnginePool, GameAnalysis, analyse_game
from .llm import LLMError, make_provider
from .pgn import ParsedGame, parse_game
from .profile import build_profile
from .session import CoachSession

TIME_CLASSES = ("all", "bullet", "blitz", "rapid", "daily")


@dataclass
class Job:
    id: str
    username: str
    status: str = "queued"  # queued | running | done | error
    stage: str = "Queued"
    progress: float = 0.0
    session_id: str | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)


class CoachService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine_error: str | None = None
        self.engine_name = "Stockfish"
        try:
            self.pool: EnginePool | None = EnginePool(settings.stockfish_path, settings.engine_workers, settings.engine_hash_mb)
            self.engine_name = self.pool.version()
        except Exception as exc:  # keep the server up and explain the problem in the UI
            self.pool, self.engine_error = None, str(exc)
        self.live = ChessComClient(settings.chesscom_user_agent, settings.cache_dir)
        self.fixtures = FixtureClient(settings.fixtures_dir)
        self.provider = make_provider(settings)
        self.jobs: dict[str, Job] = {}
        self.sessions: dict[str, CoachSession] = {}
        self._analysis_dir = Path(settings.cache_dir) / "analysis"
        self._analysis_dir.mkdir(parents=True, exist_ok=True)
        self._jobs_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
        self._engine_threads = ThreadPoolExecutor(max_workers=settings.engine_workers, thread_name_prefix="engine")
        self._locks: dict[str, threading.Lock] = {}
        self._llm_calls: dict[str, deque] = defaultdict(deque)

    # -- public-deployment guard ---------------------------------------------------------------
    def allow_llm(self, client: str) -> bool:
        """Sliding one-hour window per client; LLM_RATE_LIMIT_PER_HOUR=0 (the default) disables it."""
        limit = self.settings.llm_rate_limit_per_hour
        if not limit or self.provider is None:
            return True
        q, now = self._llm_calls[client], time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def close(self) -> None:
        self._jobs_pool.shutdown(wait=False, cancel_futures=True)
        self._engine_threads.shutdown(wait=False, cancel_futures=True)
        if self.pool:
            self.pool.close()

    # -- status ---------------------------------------------------------------------------------
    def health(self) -> dict:
        return {
            "engine": {"ok": self.pool is not None, "name": self.engine_name, "error": self.engine_error, "depth": self.settings.engine_depth},
            "llm": {"provider": self.provider.name if self.provider else None, "model": self.provider.model if self.provider else None},
            "offline": self.settings.offline,
            "demo_players": self.fixtures.usernames(),
        }

    # -- analysis jobs ---------------------------------------------------------------------------
    def start(self, username: str, n_games: int = 10, time_class: str = "all", demo: bool = False) -> Job:
        if self.pool is None:
            raise RuntimeError(self.engine_error or "Stockfish is not available")
        username = normalize_username(username)
        n_games = max(1, min(int(n_games), 30))
        time_class = time_class if time_class in TIME_CLASSES else "all"
        job = Job(id=uuid.uuid4().hex[:12], username=username)
        self.jobs[job.id] = job
        self._jobs_pool.submit(self._run, job, n_games, time_class, demo or self.settings.offline)
        return job

    def _client(self, demo: bool):
        return self.fixtures if demo else self.live

    def _run(self, job: Job, n_games: int, time_class: str, demo: bool) -> None:
        job.status, job.stage, job.progress = "running", "Fetching games from Chess.com", 0.03
        try:
            client = self._client(demo)
            try:
                profile = client.profile(job.username)
            except PlayerNotFound:
                raise
            except ChessComError:
                profile = {"username": job.username}
            try:
                stats = client.stats(job.username)
            except ChessComError:
                stats = {}
            raws = client.recent_games(job.username, n_games, time_class)
            if not raws:
                what = "standard chess games" if time_class == "all" else f"{time_class} games"
                raise ChessComError(f"No recent {what} found for {job.username}.")
            games: list[ParsedGame] = []
            for raw in raws:
                try:
                    games.append(parse_game(raw, job.username, f"G{len(games) + 1}"))
                except Exception:
                    continue  # skip malformed or variant games rather than failing the whole run
            if not games:
                raise ChessComError("Could not parse any of the fetched games.")
            job.stage, job.progress = f"Analysing {len(games)} games with {self.engine_name}", 0.1
            analyses = self._analyse_all(games, job)
            job.stage, job.progress = "Building your profile", 0.97
            prof = build_profile(job.username, profile, stats, games, analyses)
            session = CoachSession(
                sid=uuid.uuid4().hex[:12],
                username=job.username,
                games=games,
                analyses=analyses,
                profile=prof,
                engine_name=self.engine_name,
                source="fixture" if demo else "live",
            )
            self.sessions[session.sid] = session
            job.session_id, job.status, job.stage, job.progress = session.sid, "done", "Done", 1.0
        except Exception as exc:
            job.status, job.error, job.stage = "error", str(exc), "Failed"

    def _cache_path(self, g: ParsedGame) -> Path:
        key = f"{g.uuid}-{g.player_color}-d{self.settings.engine_depth}-n{self.settings.deep_nodes}-v{ANALYSIS_VERSION}"
        return self._analysis_dir / f"{key}.json"

    def _analyse_one(self, g: ParsedGame) -> GameAnalysis:
        path = self._cache_path(g)
        lock = self._locks.setdefault(str(path), threading.Lock())
        with lock:
            if path.exists():
                try:
                    return rebind(GameAnalysis.from_dict(json.loads(path.read_text())), g.gid)
                except Exception:
                    path.unlink(missing_ok=True)
            a = analyse_game(self.pool, g, self.settings.engine_depth, self.settings.deep_nodes, self.settings.refutation_nodes)
            path.write_text(json.dumps(a.to_dict()))
            return a

    def _analyse_all(self, games: list[ParsedGame], job: Job) -> dict[str, GameAnalysis]:
        out: dict[str, GameAnalysis] = {}
        futures = {self._engine_threads.submit(self._analyse_one, g): g for g in games}
        for i, fut in enumerate(as_completed(futures), 1):
            g = futures[fut]
            out[g.gid] = fut.result()
            job.progress = 0.1 + 0.87 * i / len(games)
            job.stage = f"Analysed {i}/{len(games)} games with {self.engine_name}"
        return out

    # -- coaching ---------------------------------------------------------------------------------
    def session(self, sid: str) -> CoachSession:
        s = self.sessions.get(sid)
        if not s:
            raise KeyError(sid)
        return s

    def report(self, sid: str, refresh: bool = False, on_event=None) -> dict:
        s = self.session(sid)
        if s.report and not refresh:
            return s.report
        try:
            s.report = agent.generate_report(s, self.provider, self.pool, self.settings, on_event=on_event)
        except LLMError as exc:
            fallback = agent.generate_report(s, None, self.pool, self.settings)
            fallback["error"] = f"{exc}. Showing the engine-only report instead."
            s.report = fallback
        return s.report

    def chat(self, sid: str, message: str, on_event=None) -> dict:
        s = self.session(sid)
        try:
            return agent.chat(s, self.provider, self.pool, self.settings, message, on_event=on_event)
        except LLMError as exc:
            return {"markdown": f"The language model call failed: {exc}", "verification": None, "trace": [], "error": str(exc)}

    @staticmethod
    def sse(work) -> Iterator[str]:
        """Run `work(emit)` in a thread and relay its events as server-sent events, ending with done/error."""
        q: queue.Queue = queue.Queue()

        def runner():
            try:
                q.put({"type": "done", "result": work(q.put)})
            except Exception as exc:  # surfaced to the UI instead of a broken stream
                q.put({"type": "error", "message": str(exc)})

        threading.Thread(target=runner, daemon=True).start()
        while True:
            ev = q.get()
            yield f"data: {json.dumps(ev)}\n\n"
            if ev["type"] in ("done", "error"):
                return

    # -- practice -----------------------------------------------------------------------------------
    def puzzles(self, sid: str) -> list[dict]:
        return practice.puzzle_list(self.session(sid))

    def puzzle_attempt(self, sid: str, ref: str, uci: str) -> dict:
        return practice.attempt(self.session(sid), self.pool, ref, uci, self.settings.refutation_nodes)

    def puzzle_hint(self, sid: str, ref: str, level: int) -> dict:
        return practice.hint(self.session(sid), ref, level)

    # -- views --------------------------------------------------------------------------------------
    def session_view(self, sid: str) -> dict:
        s = self.session(sid)
        games = []
        for g in s.games:
            a = s.analyses[g.gid]
            games.append(
                {
                    "gid": g.gid,
                    "url": g.url,
                    "date": g.date,
                    "time_class": g.time_class,
                    "time_control": g.time_control,
                    "white": g.white,
                    "black": g.black,
                    "white_rating": g.white_rating,
                    "black_rating": g.black_rating,
                    "player_color": g.player_color,
                    "opponent": g.opponent,
                    "opponent_rating": g.opponent_rating,
                    "outcome": g.outcome,
                    "end_reason": g.end_reason,
                    "opening": g.opening,
                    "accuracy": a.accuracy,
                    "opponent_accuracy": a.opponent_accuracy,
                    "chesscom_accuracy": g.chesscom_accuracy,
                    "counts": a.counts,
                    "start_fen": g.start_fen,
                    # Win chance for the coached player after each ply (index 0 = start position).
                    "win_curve": [round(e.win(g.player_color), 1) for e in a.evals],
                    # Eval label for the eval bar after each ply, from the coached player's side.
                    "eval_curve": [_eval_label(e, g.player_color) for e in a.evals],
                    "plies": [
                        {
                            "label": p.label,
                            "san": p.san,
                            "uci": p.uci,
                            "color": p.color,
                            "fen": p.fen_after,
                            "clock": p.clock,
                            "time_spent": p.time_spent,
                            "cls": a.moves[p.index].classification,
                            "best": a.moves[p.index].best_san,
                            "best_uci": a.evals[p.index].best_uci,
                            "eval_after": a.moves[p.index].eval_after,
                        }
                        for p in g.plies
                    ],
                    "moments": [m.ref for m in a.moments],
                }
            )
        moments = []
        for m in s.moments():
            d = asdict(m)
            d["best_san"] = m.best_san
            for line, dl in zip(m.best_lines, d["best_lines"]):
                dl["eval_str"] = line.eval_str
            if m.refutation:
                d["refutation"]["eval_str"] = m.refutation.eval_str
            moments.append(d)
        return {
            "sid": s.sid,
            "username": s.username,
            "source": s.source,
            "engine": s.engine_name,
            "profile": s.profile,
            "games": games,
            "moments": moments,
            "llm": self.health()["llm"],
            "report": s.report,
        }

    def board_svg(
        self, sid: str, gid: str, ply: int, mode: str = "after", size: int = 400,
        attempt: str | None = None, reply: str | None = None, reveal: str | None = None, mark: str | None = None,
    ) -> str:
        """mode=after: the position after `ply`, with a green arrow for the better move if `ply` was an error.
        mode=decision: the position BEFORE `ply`, with the played move (red) and the engine's choice (green)."""
        s = self.session(sid)
        g = s.game(gid)
        if g is None:
            raise KeyError(gid)
        a = s.analyses[g.gid]
        ply = max(-1, min(ply, len(g.plies) - 1))
        arrows = []

        def best_for(i: int) -> str | None:
            m = s.moment_at(g.gid, i)
            if m and m.best_lines and m.best_lines[0].uci:
                return m.best_lines[0].uci[0]
            return a.evals[i].best_uci

        def arrow(uci: str, color: str) -> chess.svg.Arrow:
            mv = chess.Move.from_uci(uci)
            return chess.svg.Arrow(mv.from_square, mv.to_square, color=color)

        def legal(board: chess.Board, uci: str | None) -> chess.Move | None:
            """A caller-supplied move, or None if it is malformed or illegal here (never an error)."""
            if not uci:
                return None
            try:
                mv = chess.Move.from_uci(uci)
            except ValueError:
                return None
            if board.is_legal(mv):
                return mv
            # Bare pawn promotions ("e7e8") arrive without a piece from the board UI.
            promo = chess.Move(mv.from_square, mv.to_square, promotion=chess.QUEEN)
            return promo if board.is_legal(promo) else None

        if mode == "puzzle" and ply >= 0:
            # Practice: the position before the player's mistake, with no engine hints drawn.
            p = g.plies[ply]
            board = chess.Board(p.fen_before)
            last = chess.Move.from_uci(g.plies[ply - 1].uci) if ply > 0 else None
            mv = legal(board, attempt)
            if mv:
                board.push(mv)
                last = mv
                r = legal(board, reply)
                if r:
                    arrows.append(arrow(r.uci(), "#FA412Dcc"))
            else:
                rv = legal(board, reveal)
                if rv:
                    arrows.append(arrow(rv.uci(), "#81B64Ccc"))
            fill = {}
            if mark:
                try:
                    fill[chess.parse_square(mark)] = "#81B64C99"
                except ValueError:
                    pass
            return chess.svg.board(
                board,
                orientation=chess.WHITE if g.player_color == "white" else chess.BLACK,
                lastmove=last,
                arrows=arrows,
                fill=fill,
                check=board.king(board.turn) if board.is_check() else None,
                size=size,
                coordinates=False,
                colors={"square light": "#EBECD0", "square dark": "#739552", "square light lastmove": "#F5F682", "square dark lastmove": "#B9CA43"},
            )
        if mode == "decision" and ply >= 0:
            p = g.plies[ply]
            board = chess.Board(p.fen_before)
            last = chess.Move.from_uci(g.plies[ply - 1].uci) if ply > 0 else None
            bu = best_for(ply)
            arrows.append(arrow(p.uci, "#FA412Dbb"))
            if bu and bu != p.uci:
                arrows.append(arrow(bu, "#81B64Ccc"))
        elif ply < 0:
            board, last = chess.Board(g.start_fen), None
        else:
            p = g.plies[ply]
            board, last = chess.Board(p.fen_after), chess.Move.from_uci(p.uci)
            bu = best_for(ply)
            if bu and bu != p.uci and a.moves[ply].classification in ("inaccuracy", "mistake", "miss", "blunder"):
                arrows.append(arrow(bu, "#81B64Ccc"))
        check = board.king(board.turn) if board.is_check() else None
        # Chess.com's "green" board theme tokens (#EBECD0 / #739552, #ffff33 highlight at 50%).
        # Pieces are python-chess's open-licensed set; coordinates are drawn inside the squares by the UI.
        return chess.svg.board(
            board,
            orientation=chess.WHITE if g.player_color == "white" else chess.BLACK,
            lastmove=last,
            arrows=arrows,
            check=check,
            size=size,
            coordinates=False,
            colors={
                "square light": "#EBECD0",
                "square dark": "#739552",
                "square light lastmove": "#F5F682",
                "square dark lastmove": "#B9CA43",
                "arrow green": "#81B64Ccc",
            },
        )


def _eval_label(e, color: str) -> str:
    if e.terminal == "checkmate":
        return "1-0" if (e.cp or 0) > 0 else "0-1"
    if e.terminal == "draw":
        return "½-½"
    cp, mate = e.pov(color)
    if mate is not None:
        return f"M{abs(mate)}" if mate > 0 else f"-M{abs(mate)}"
    return f"{(cp or 0) / 100:+.1f}"


def rebind(a: GameAnalysis, gid: str) -> GameAnalysis:
    """Cached analyses are keyed by game, not by position in this session's list: relabel the ids."""
    a.gid = gid
    for m in a.moments:
        m.gid = gid
        m.ref = f"{gid} {m.ref.split(' ', 1)[1]}"
    return a
