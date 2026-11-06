# Design notes

## The principle

A chess coach has two jobs: knowing what is true on the board, and knowing what to say about it. LLMs are good at the second and unreliable at the first: they misread positions, invent moves and quote evaluations that were never computed. So the system splits the jobs.

| Responsibility | Owner |
|---|---|
| Evaluations, best moves, refutations, legality | Stockfish |
| Classifying moves, counting, averaging, detecting patterns | Python |
| Choosing what matters, explaining why, writing a plan, answering questions | LLM |
| Checking the LLM stayed inside the facts | Python (verifier) |

Everything below follows from that split.

## Pipeline

1. **Fetch** (`chesscom.py`). Monthly archives are walked newest first until N standard-chess games are collected. Requests are serial with a descriptive User-Agent, as the Published Data API asks, and HTTP 429 is retried with backoff. Past months are cached forever (they never change) and the current month for 5 minutes.
2. **Parse** (`pgn.py`). Each game becomes a list of plies from the coached player's perspective, with clock left and time spent per move (from `[%clk]`, including increments) and an opening name and family parsed from Chess.com's `ECOUrl` slug.
3. **Fast pass** (`engine.py`). Every position at depth 12, about 45 ms each. Win chance comes from Lichess's published logistic model. Moves are labelled with Chess.com's expected-points bands: 5 points or more is an inaccuracy, 10 a mistake, 20 a blunder.
4. **Deep re-check.** Up to 5 of the player's worst candidates per game get a 3-line search (300k nodes) and a search for the opponent's best reply after the played move (150k nodes). If the deeper search says the move was best, or within 5 points of it, the candidate is dropped and the per-move label is corrected. On the 6 demo games the deep check confirmed all 18 fast-pass mistakes and blunders, so there its value is the more precise cost, the alternatives and the punishing line rather than filtering. It remains a guard against depth-12 noise. At most 3 moments per game are kept.
5. **Reproducibility.** Depth and node limits (never time limits), plus a `ucinewgame` hash reset per game, make every verdict identical run to run. A test enforces this. It matters because an eval of LLM faithfulness is meaningless if the ground truth shifts between runs.
6. **Motifs** (`motifs.py`). Cheap, explainable tags from the board and the engine lines: `hangs_piece` (the reply captures an undefended piece worth 3 or more), `allows_fork` (the reply attacks two or more valuable pieces), `loses_material` and `missed_material_gain` (net material over the first 4 plies of a line), `allows_mate`, `missed_mate`, `best_was_capture`, `best_was_check`, `time_pressure` (under max(15 s, 10% of base) left), `played_quickly` (2 s or less while not short of time), `long_think`, and `was_winning`. Each tag comes with a human-readable note that names the squares and the line.
7. **Profile** (`profile.py`). Accuracy overall and by phase, errors per game and by phase, time-related error counts, thrown wins (80% or more after move 10 without winning), comebacks, patterns with references, and opening results by color and family.
8. **Analysis cache.** Keyed by game UUID, player color, depth, node budget and an analysis version, so the same game is never analysed twice. IDs like `G1` are rebound when a cached analysis is reused in another session.

Typical cost: 6 blitz games take about 20 s on 2 cores the first time and are instant afterwards.

## Context strategy

The context (`context.py`) is structured plain text, about 15k characters (roughly 4k tokens) for 6 games, in five blocks: PLAYER, SAMPLE, ENGINE SUMMARY, GAMES and CRITICAL MOMENTS. The choices:

- **No raw PGN.** The model gets conclusions (verdicts, lines, tags), not data it would have to interpret. The ablation in `evals/` measures what happens when it only gets the PGN.
- **Citable IDs that carry their own check.** `[[G1 22...Qc7]]` contains the move actually played, so the verifier can tell a real citation from a hallucinated one. The same string becomes a clickable board in the UI, so grounding and UX are one mechanism.
- **Player point of view everywhere.** All evals and win chances are from the coached player's side.
- **Plain-language anchors next to numbers.** "cost 24 points of win chance" sits beside "+1.6 -> -1.1", so the model can explain without quoting numbers.
- **Bounded size.** At most 24 moments (the costliest) go into the prompt, and the rest are reachable through tools. With 20 games the context stays under about 8k tokens.
- **Prompt caching.** For Anthropic, the system block (rules plus context) is marked `cache_control: ephemeral`, so chat follow-ups reuse it cheaply.

## Prompting

`prompts.py` has a shared GROUND RULES block in which each rule closes a known failure mode:

| Rule | Failure it prevents |
|---|---|
| Engine data is the truth; call a tool instead of calculating | Misread positions, invented lines |
| Cite every move as `[[G# move]]` with the move actually played | Unfalsifiable or hallucinated references |
| Only mention moves present in context or tool output | Plausible-looking illegal moves |
| Only quote evals that appear verbatim | Made-up numbers |
| Statistics only from ENGINE SUMMARY | Arithmetic errors, invented percentages |
| Explain with tags, the reply and the clock; name pieces and squares | Generic advice ("be more careful") |
| Say so when the data cannot answer | Confident filler |

The report task fixes a structure (big picture, 2 or 3 patterns with evidence and a habit for each, moments to replay, what is working, a 7-day plan) and a 550-word cap, which makes outputs comparable across runs and easy to scan. The LEVEL guide changes depth and vocabulary from beginner to master. Chat reuses the rules and adds the tool descriptions and the already-delivered report, so answers stay consistent with it.

## Tools

| Tool | Purpose |
|---|---|
| `get_game_moves(game_id, from_move, to_move)` | Move list with clocks and per-move verdicts (30 moves per call) |
| `get_moment(ref)` | Full detail for any move, including non-critical ones (runs Stockfish if needed) |
| `analyze_position(ref, before/after)` | Stockfish's top 3 lines for a position |
| `evaluate_move(ref, move)` | Legality, eval versus the engine's best and versus the game move, plus the critical reply |

Tools take move references instead of FENs because models often corrupt FEN strings. Errors are returned as text (an illegal move returns the list of legal moves), so the model can recover. The loop is capped at 6 tool rounds. Every position and line a tool produces is recorded as evidence the verifier will accept.

## Verifier

`verify.py` splits the answer into paragraphs and list items. Citations in a paragraph anchor it to positions: the move's before and after positions, a few game moves on either side, the engine's top lines and the punishing reply. Then:

- **Citations:** the game exists, the move number and side exist, and the SAN matches the move played.
- **Moves:** each SAN token is legal in an anchored position or a tool-produced position. Unanchored mentions must be legal somewhere in the analysed games.
- **Recommendations:** in sentences that recommend ("better", "should have", "instead", "engine preferred"...), a move legal at the decision point must be one of the engine's top 3 there or have been endorsed by `evaluate_move` (within 5 points of best). A recommended move that is not legal at the decision point must at least appear in an engine line.
- **Evaluations:** every `+1.7` or `#3` must appear in the context or a tool output.

If anything is flagged, the model gets one repair turn listing the exact problems. The repaired answer is kept only if it scores at least as well. The engine-only template report passes the same verifier at 100%, which doubles as a regression test of the verifier itself; it already caught a real bug (`str.capitalize()` lowercasing SAN in a note).

## Trade-offs

| Decision | Alternative | Why |
|---|---|---|
| Fast depth-12 pass plus a targeted deep pass | Deep search everywhere | About 10x cheaper; depth only matters where the player went wrong |
| Lichess win% and accuracy formulas | Chess.com's CAPS | CAPS is proprietary. Both numbers are shown and the difference is labelled |
| Deterministic motif tags | Ask the LLM to spot tactics | LLMs misread boards; tags are testable and auditable |
| Lexical verifier plus one repair | LLM-as-judge for grounding | Deterministic, free and exact for the claims it covers; the judge approach is listed as next |
| Raw-HTTP providers | Vendor SDKs | Two small adapters, no version drift, easy to mock in tests |
| Template report without a key | Require a key | Reviewers can run everything except chat with zero setup |
| In-memory sessions, disk cache for analysis | Database | Enough for a single-user demo; the expensive part is cached |
| Server-rendered SVG boards, vanilla JS | React plus a board library | No build step, no CDN, works offline in Docker |
| Chess.com design language, original implementation | Pixel copy with Chess.com assets | Colors, board theme, radii and Game Review conventions come from Chess.com's public CSS tokens so the tool feels native to their product, but logos, the Neo piece set, the Chess Sans/Chess Note fonts and icon art are proprietary, so open-licensed pieces, system fonts and standard annotation glyphs are used instead |
| Real games in `fixtures/` | Synthetic games | Realistic edge cases (clocks, increments, checkmates) for tests and an offline demo |

## Known gaps

See the Limitations section of the README. The most important: the verifier cannot judge prose claims, the motif tags are one-reply heuristics, and the eval measures faithfulness to Stockfish rather than whether the coaching helps a player improve.
