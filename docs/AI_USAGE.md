# Time breakdown and AI usage

## Time breakdown

<!-- TODO(Manas): fill in honestly before submitting. -->

| Part | Time |
|---|---|
| Choosing the track and design | TODO |
| Data collection and engine analysis pipeline | TODO |
| Context, prompts, tools and verifier | TODO |
| Web UI | TODO |
| Tests, eval harness, Docker, docs | TODO |
| **Total** | TODO |

## AI usage

This project was built with Claude (Anthropic) as a coding agent. The agent wrote most of the code, tests and docs from my direction and design decisions, and I reviewed the result.

- Agent transcript: TODO(Manas): add the shared transcript link or export.

Notes on the process:

- The agent pulled the fixture games through a browser on my machine, because the build sandbox could not reach api.chess.com. The PGNs in `fixtures/` are verbatim API responses, and a check confirmed that each replays exactly to the `CurrentPosition` Chess.com recorded.
- The LLM paths were tested against mocked Anthropic and OpenAI HTTP responses and a scripted model, because the build environment had no API key. `make eval` runs the real comparison.
