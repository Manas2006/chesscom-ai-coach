"""Pre-compute the offline demo analysis into .cache/ (run during `docker build`).

A fresh container, including a small free-tier host, then serves the demo instantly instead of
running Stockfish over every position on first click.
"""

from __future__ import annotations

import time

from .config import Settings
from .service import CoachService


def main() -> None:
    settings = Settings.from_env()
    settings.anthropic_api_key = settings.openai_api_key = None
    svc = CoachService(settings)
    try:
        for username in svc.fixtures.usernames():
            t0 = time.time()
            job = svc.start(username, 30, "all", demo=True)
            while job.status in ("queued", "running"):
                time.sleep(0.2)
            print(f"warmed {username}: {job.status} in {time.time() - t0:.1f}s {job.error or ''}".strip())
            if job.status != "done":
                raise SystemExit(1)
    finally:
        svc.close()


if __name__ == "__main__":
    main()
