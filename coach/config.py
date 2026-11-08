"""Runtime settings, all overridable through environment variables (see .env.example)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so `python -m coach` works without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _find_stockfish() -> str | None:
    explicit = os.environ.get("STOCKFISH_PATH")
    if explicit:
        return explicit
    for candidate in ("stockfish", "/usr/games/stockfish", "/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish"):
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    return None


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    stockfish_path: str | None = None
    engine_depth: int = 12  # fast pass over every position
    # Node limits (not time limits) plus a hash reset per game make every verdict reproducible.
    deep_nodes: int = 300_000  # per critical moment (multi-PV re-check), about 0.6s
    refutation_nodes: int = 150_000  # to find the opponent's punishing reply
    engine_workers: int = 2
    engine_hash_mb: int = 64

    llm_provider: str = "auto"  # auto | anthropic | openai | none
    anthropic_model: str = "claude-sonnet-5-5"
    openai_model: str = "gpt-5-mini"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    llm_max_tokens: int = 8000  # a report is ~600 words; the headroom is for models that spend output tokens on reasoning
    llm_timeout_s: float = 90.0
    max_tool_rounds: int = 6
    llm_rate_limit_per_hour: int = 0  # per client IP; set it for public deployments

    cache_dir: Path = field(default_factory=lambda: ROOT / ".cache")
    fixtures_dir: Path = field(default_factory=lambda: ROOT / "fixtures")
    chesscom_user_agent: str = (
        "chesscom-ai-coach/1.0 (take-home project; +https://github.com/Manas2006/chesscom-ai-coach)"
    )
    offline: bool = False  # serve fixtures only (no network)
    demo_player: str | None = None  # fixture shown as the sample review; None = the fixture marked default

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        s = cls()
        s.stockfish_path = _find_stockfish()
        s.engine_depth = _int("ENGINE_DEPTH", s.engine_depth)
        s.deep_nodes = _int("ENGINE_DEEP_NODES", s.deep_nodes)
        s.refutation_nodes = _int("ENGINE_REFUTATION_NODES", s.refutation_nodes)
        s.engine_workers = max(1, _int("ENGINE_WORKERS", min(4, os.cpu_count() or 2)))
        s.engine_hash_mb = _int("ENGINE_HASH_MB", s.engine_hash_mb)
        s.llm_provider = os.environ.get("LLM_PROVIDER", s.llm_provider).lower()
        s.anthropic_model = os.environ.get("ANTHROPIC_MODEL", s.anthropic_model)
        s.openai_model = os.environ.get("OPENAI_MODEL", s.openai_model)
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY") or None
        s.openai_api_key = os.environ.get("OPENAI_API_KEY") or None
        s.llm_max_tokens = _int("LLM_MAX_TOKENS", s.llm_max_tokens)
        s.max_tool_rounds = _int("LLM_MAX_TOOL_ROUNDS", s.max_tool_rounds)
        s.llm_rate_limit_per_hour = _int("LLM_RATE_LIMIT_PER_HOUR", s.llm_rate_limit_per_hour)
        if os.environ.get("COACH_CACHE_DIR"):
            s.cache_dir = Path(os.environ["COACH_CACHE_DIR"])
        if os.environ.get("CHESSCOM_USER_AGENT"):
            s.chesscom_user_agent = os.environ["CHESSCOM_USER_AGENT"]
        s.offline = os.environ.get("COACH_OFFLINE", "").lower() in {"1", "true", "yes"}
        s.demo_player = os.environ.get("DEMO_PLAYER") or None
        return s
