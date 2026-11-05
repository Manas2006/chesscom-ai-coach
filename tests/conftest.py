import time

import pytest

from coach.config import Settings
from coach.service import CoachService


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    s = Settings.from_env()
    s.cache_dir = tmp_path_factory.mktemp("cache")
    s.anthropic_api_key = None
    s.openai_api_key = None
    s.llm_provider = "none"
    s.engine_workers = 2
    if not s.stockfish_path:
        pytest.skip("Stockfish is not installed", allow_module_level=True)
    return s


@pytest.fixture(scope="session")
def service(settings):
    svc = CoachService(settings)
    yield svc
    svc.close()


@pytest.fixture(scope="session")
def g1_moment(session):
    """22...Qc7 in G1 (a mistake in every Stockfish version we tried) plus engine-derived moves around it, so
    tests never hard-code one engine version's preferences."""
    import chess

    g = session.games[0]
    m = session.moment_at(g.gid, g.find_ply(22, "black").index)
    assert m is not None
    top = {l.san[0] for l in m.best_lines if l.san}
    board = chess.Board(m.fen_before)
    outsider = next(board.san(mv) for mv in board.legal_moves if board.san(mv) not in top and board.san(mv) != m.played_san and "x" not in board.san(mv) and "+" not in board.san(mv))
    return {"ref": m.ref, "best": m.best_san, "outsider": outsider, "moment": m}


@pytest.fixture(scope="session")
def session(service):
    """The offline demo: 6 real GothamChess blitz games, analysed once for the whole test run."""
    job = service.start("gothamchess", 6, "all", demo=True)
    deadline = time.time() + 240
    while job.status in ("queued", "running") and time.time() < deadline:
        time.sleep(0.25)
    assert job.status == "done", job.error
    return service.session(job.session_id)
