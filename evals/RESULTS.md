# Eval results (claude-sonnet-5-5, 2026-11-08T22:09:42+00:00)

4 run(s) over: bobthegreatest123 (8 games), gothamchess (6 games)

| Metric | Ungrounded (PGN only) | Grounded, first draft | Grounded + verify/repair |
|---|---|---|---|
| grounding_score | 0.944 | 0.981 | 1.0 |
| citation_validity | 1.0 | 0.969 | 1.0 |
| move_legality | 0.912 | 1.0 | 1.0 |
| recommendation_agreement | 0.5 | 0.938 | 1.0 |
| eval_support | None | 1.0 | 1.0 |
| citations | 19.25 | 16.5 | 16.0 |
| words | 636.75 | 664.75 | 664.75 |
| latency_s | 47.55 | 18.2 | 18.2 |
| tokens | 10127.0 | 22491.0 | 22491.0 |

Flagged items (first run per player):

- bobthegreatest123 / Ungrounded (PGN only): none
- bobthegreatest123 / Grounded, first draft: none
- bobthegreatest123 / Grounded + verify/repair: none
- gothamchess / Ungrounded (PGN only): 18...Bc3, 19.Bxc3, dxc3, Nc5, 21.Qxc3, axb5
- gothamchess / Grounded, first draft: none
- gothamchess / Grounded + verify/repair: none

Rates are passed/total for each check type; None means the answer made no claims of that type.
Raw outputs are in evals/samples/.
