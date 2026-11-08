.PHONY: up run test eval eval-docker eval-dry help

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

up:  ## one command, only needs Docker: build and serve on http://localhost:8000
	docker compose up --build

run:  ## run locally (needs uv and a Stockfish binary on PATH)
	uv run python -m coach

test:  ## run the test suite (needs Stockfish)
	uv run pytest -q

eval:  ## grounded vs ungrounded eval on the offline demo games (needs an API key in .env)
	uv run python -m evals.run_eval

eval-docker:  ## same eval, but inside the Docker image (needs only Docker and an API key in .env); writes evals/RESULTS.md
	docker compose run --rm -v "$$(pwd)/evals:/app/evals" coach python -m evals.run_eval

eval-dry:  ## exercise the eval harness without an API key
	uv run python -m evals.run_eval --dry-run
