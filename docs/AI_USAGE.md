# Time breakdown and AI usage

## Time breakdown

Wall-clock time of the working sessions (America/Chicago), taken from the agent transcript and the
commit times. I was present and directing throughout; the agent did the typing. The total is well
over the suggested four hours. I chose to spend the extra time on the verifier, the tests and the UI
rather than stop at a first working version.

| Session | What happened | Time |
|---|---|---|
| Thu Nov 5, 16:20 to 16:30 | Read the brief, chose Option B, settled the design: engine decides, LLM explains, verifier checks | 0.2 h |
| Thu Nov 5, 16:30 to 17:45 | v1 end to end: API client and fixtures, PGN parsing with clocks, Stockfish pipeline and motif tags, context builder, prompts, Anthropic/OpenAI adapters, tool-using agent, verifier with repair, FastAPI + first UI, tests, eval harness, Docker, CI, README and DESIGN | 1.3 h |
| Thu Nov 5, evening to 20:45 | Chess.com design language and Game Review layout (eval bar, player bars, classifications, figurine move list) | 1.5 h |
| Thu Nov 5, 20:45 to 22:45 | Practice mode, streaming reports and chat, Miss classification, per-move "ask the coach", Render deploy config | 2.0 h |
| Sat Nov 7, late to Sun Nov 8, 01:20 | Hardening pass: 110 edge-case tests across every module and the bugs they found (checkmate scored 97.5%, non-reproducible hash state, empty answers at the tool budget, API edge cases) | 2.0 h |
| Sun Nov 8, 13:50 to 15:40 | Deploy to Render, API credits and key, sample review from my own games with a frozen report, token-limit truncation fix, repo history cleanup, eval workflow | 1.8 h |
| **Total** | | **about 9 h** |

Roughly by part: design 0.2 h; data collection and engine pipeline 0.6 h; context, prompts, tools and
verifier 0.9 h; web UI 3.3 h; tests, eval harness, Docker, CI and docs 2.5 h; deployment and sample
data 1.5 h.

## AI usage

This project was built with Claude (Anthropic) as a coding agent, in the Claude app with Claude Code
tools. The agent wrote most of the code, tests and docs from my direction and design decisions, and I
reviewed the result at each step. The grounded-coach idea (Stockfish decides what is true, the model
explains, a verifier checks every move and citation) came out of the first design discussion.

- Agent transcript: TODO(Manas): add the shared transcript link.

Notes on the process:

- The agent pulled the fixture games through a browser on my machine, because the build sandbox could
  not reach api.chess.com. The PGNs in `fixtures/` are verbatim API responses, and a check confirmed
  that each replays exactly to the `CurrentPosition` Chess.com recorded.
- The LLM paths were first tested against mocked Anthropic and OpenAI HTTP responses and a scripted
  model, because the build environment had no API key. The real comparison is `evals/RESULTS.md`,
  produced by the `eval` GitHub Actions workflow with the key held as a repository secret.
- The first live report on the deployment was cut off at the 2,000-token output limit; that bug was
  found by using the product, not by the tests, and led to the truncation flag and the higher limit.
