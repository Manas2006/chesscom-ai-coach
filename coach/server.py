"""HTTP API + static web UI."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chesscom import PlayerNotFound
from .config import Settings
from .service import CoachService

STATIC = Path(__file__).resolve().parent / "static"


class AnalyzeRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=40)
    games: int = Field(10, ge=1, le=30)
    time_class: str = "all"
    demo: bool = False


class AttemptRequest(BaseModel):
    ref: str = Field(..., max_length=40)
    uci: str = Field(..., min_length=4, max_length=5)


class HintRequest(BaseModel):
    ref: str = Field(..., max_length=40)
    level: int = Field(1, ge=1, le=3)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


def create_app(service: CoachService | None = None) -> FastAPI:
    svc = service or CoachService(Settings.from_env())
    app = FastAPI(title="Chess.com AI Coach", version="1.0.0")
    app.state.service = svc

    @app.get("/api/health")
    def health():
        return svc.health()

    @app.post("/api/analyze")
    def analyze(req: AnalyzeRequest):
        try:
            job = svc.start(req.username, req.games, req.time_class, req.demo)
        except PlayerNotFound as exc:
            raise HTTPException(404, str(exc))
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        j = svc.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "unknown job")
        return {"status": j.status, "stage": j.stage, "progress": round(j.progress, 3), "session_id": j.session_id, "error": j.error}

    def _session_or_404(sid: str):
        try:
            return svc.session(sid)
        except KeyError:
            raise HTTPException(404, "Session expired. Please analyse the player again.")

    @app.get("/api/sessions/{sid}")
    def session(sid: str):
        _session_or_404(sid)
        return svc.session_view(sid)

    def _client(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")

    def _rate_limit(request: Request) -> None:
        if not svc.allow_llm(_client(request)):
            raise HTTPException(429, "This demo limits AI coach requests per hour. Please try again later.")

    SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    @app.post("/api/sessions/{sid}/report")
    def report(sid: str, request: Request, refresh: bool = False):
        s = _session_or_404(sid)
        if refresh or not s.report:  # a cached report costs nothing
            _rate_limit(request)
        return svc.report(sid, refresh=refresh)

    @app.post("/api/sessions/{sid}/report/stream")
    def report_stream(sid: str, request: Request, refresh: bool = False):
        s = _session_or_404(sid)
        if refresh or not s.report:
            _rate_limit(request)
        work = lambda emit: svc.report(sid, refresh=refresh, on_event=emit)  # noqa: E731
        return StreamingResponse(svc.sse(work), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/sessions/{sid}/chat")
    def chat(sid: str, req: ChatRequest, request: Request):
        _session_or_404(sid)
        _rate_limit(request)
        return svc.chat(sid, req.message)

    @app.post("/api/sessions/{sid}/chat/stream")
    def chat_stream(sid: str, req: ChatRequest, request: Request):
        _session_or_404(sid)
        _rate_limit(request)
        work = lambda emit: svc.chat(sid, req.message, on_event=emit)  # noqa: E731
        return StreamingResponse(svc.sse(work), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/sessions/{sid}/puzzles")
    def puzzles(sid: str):
        _session_or_404(sid)
        return svc.puzzles(sid)

    @app.post("/api/sessions/{sid}/puzzles/attempt")
    def puzzle_attempt(sid: str, req: AttemptRequest):
        _session_or_404(sid)
        try:
            return svc.puzzle_attempt(sid, req.ref, req.uci)
        except KeyError:
            raise HTTPException(404, "unknown puzzle")

    @app.post("/api/sessions/{sid}/puzzles/hint")
    def puzzle_hint(sid: str, req: HintRequest):
        _session_or_404(sid)
        try:
            return svc.puzzle_hint(sid, req.ref, req.level)
        except KeyError:
            raise HTTPException(404, "unknown puzzle")

    UCI = "^[a-h][1-8][a-h][1-8][qrbn]?$"

    @app.get("/api/sessions/{sid}/board.svg")
    def board(
        sid: str,
        game: str,
        ply: int = -1,
        mode: str = Query("after", pattern="^(after|decision|puzzle)$"),
        size: int = Query(400, ge=120, le=900),
        attempt: str | None = Query(None, pattern=UCI),
        reply: str | None = Query(None, pattern=UCI),
        reveal: str | None = Query(None, pattern=UCI),
        mark: str | None = Query(None, pattern="^[a-h][1-8]$"),
    ):
        _session_or_404(sid)
        try:
            svg = svc.board_svg(sid, game, ply, mode=mode, size=size, attempt=attempt, reply=reply, reveal=reveal, mark=mark)
        except KeyError:
            raise HTTPException(404, "unknown game")
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=3600"})

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(Exception)
    def unhandled(_, exc: Exception):  # pragma: no cover - last-resort guard
        return JSONResponse({"detail": f"Internal error: {exc}"}, status_code=500)

    return app


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n  Chess.com AI Coach running at http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}\n")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
