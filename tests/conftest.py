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
def session(service):
    """The offline demo: 6 real GothamChess blitz games, analysed once for the whole test run."""
    job = service.start("gothamchess", 6, "all", demo=True)
    deadline = time.time() + 240
    while job.status in ("queued", "running") and time.time() < deadline:
        time.sleep(0.25)
    assert job.status == "done", job.error
    return service.session(job.session_id)
