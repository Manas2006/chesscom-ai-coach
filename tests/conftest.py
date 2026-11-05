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
def crit(session):
    """The costliest critical moment found by whatever Stockfish build is installed, plus moves derived from it,
    so no test hard-codes one engine version's opinions."""
    import chess

    from coach.context import build_context
    from coach.session import _line_fens

    m = max(session.moments(), key=lambda x: x.win_drop)
    target = session.resolve_ref(m.ref)
    board = chess.Board(m.fen_before)
    sans = sorted(board.san(mv) for mv in board.legal_moves)
    top = {l.san[0] for l in m.best_lines if l.san}
    outsider = next(s for s in sans if s not in top and s != m.played_san and "x" not in s and "+" not in s and s[0].isupper())
    wrong = next(s for s in sans if s != m.played_san and s[0].isupper())
    num, dots = m.ref.split(" ", 1)[1].split(m.played_san)[0], None
    evidence = session.evidence_fens(target)

    def legal_anywhere(san):
        for fen in evidence:
            try:
                chess.Board(fen).parse_san(san)
                return True
            except ValueError:
                pass
        return False

    invented = next(s for s in ["Nxh8", "Qxa1", "Kxa8", "Rxh1", "Bxa1", "Nxa1", "Kxh1"] if not legal_anywhere(s))
    in_lines = {x for l in m.best_lines for x in l.san} | set(m.refutation.san if m.refutation else []) | {c.split(".")[-1] for c in m.continuation}
    nowhere = None
    for fen in evidence:
        b = chess.Board(fen)
        for mv in b.legal_moves:
            s = b.san(mv)
            if s[0] in "QRBN" and "x" not in s and s not in in_lines and s not in sans:
                nowhere = s
                break
        if nowhere:
            break
    context = build_context(session)
    fake_eval = next(f"+{v / 10:.1f}" for v in range(73, 99) if f"+{v / 10:.1f}" not in context)
    return {
        "ref": m.ref,
        "best": m.best_san,
        "played": m.played_san,
        "label": m.ref.split(" ", 1)[1],
        "wrong_ref": f"{m.gid} {num}{wrong}",
        "outsider": outsider,
        "invented": invented,
        "nowhere": nowhere,
        "fake_eval": fake_eval,
        "moment": m,
    }


@pytest.fixture(scope="session")
def session(service):
    """The offline demo: 6 real GothamChess blitz games, analysed once for the whole test run."""
    job = service.start("gothamchess", 6, "all", demo=True)
    deadline = time.time() + 240
    while job.status in ("queued", "running") and time.time() < deadline:
        time.sleep(0.25)
    assert job.status == "done", job.error
    return service.session(job.session_id)
