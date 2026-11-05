"""Does grounding work? An ablation of the coach's context strategy, scored by the same verifier.

Conditions (same games, same model, same report task):
  ungrounded   the LLM sees only the PGN move lists (what a naive "paste your games into ChatGPT" coach gets)
  grounded     the LLM sees the engine context and may call Stockfish tools (first draft, before any repair)
  + repair     the grounded draft after one verify-then-repair pass (what the app actually shows)

Metrics come from coach.verify: citation validity, move legality, recommendation agreement with
Stockfish, and evaluation support. They measure faithfulness to the engine, not coaching quality;
see DESIGN.md for what they do and do not capture.

Usage:
  python -m evals.run_eval                              # offline demo games, LLM from .env
  python -m evals.run_eval --live hikaru erik --games 8 # live Chess.com players
  python -m evals.run_eval --dry-run                    # no API key: exercises the harness only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from coach import prompts
from coach.agent import Toolbox, _respond, run_agent, template_report
from coach.config import Settings
from coach.context import build_baseline_context, build_context
from coach.llm import LLMResponse, ScriptedProvider, make_provider
from coach.service import CoachService
from coach.verify import verify

OUT = Path(__file__).resolve().parent
BASELINE_TASK = prompts.REPORT_TASK.replace(
    "with the\nkey numbers from ENGINE SUMMARY", "with key numbers"
).replace("Use tools only if you genuinely need information that is not in CONTEXT.", "")


def _metrics(v: dict, text: str, latency: float, usage: dict) -> dict:
    def rate(c):
        return round(c["ok"] / c["total"], 3) if c["total"] else None

    return {
        "grounding_score": v["score"],
        "claims_checked": v["checked"],
        "citation_validity": rate(v["citations"]),
        "citations": v["citations"]["total"],
        "move_legality": rate(v["moves"]),
        "recommendation_agreement": rate(v["recommendations"]),
        "eval_support": rate(v["evals"]),
        "flagged": v["flagged"][:12],
        "words": len(text.split()),
        "latency_s": round(latency, 1),
        "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }


def _analyse(service: CoachService, username: str, games: int, demo: bool):
    job = service.start(username, games, "all", demo=demo)
    while job.status in ("queued", "running"):
        time.sleep(0.3)
    if job.status != "done":
        raise RuntimeError(f"{username}: {job.error}")
    return service.session(job.session_id)


def run(args) -> dict:
    settings = Settings.from_env()
    service = CoachService(settings)
    provider = None if args.dry_run else make_provider(settings, args.provider)
    if provider is None and not args.dry_run:
        sys.exit("No LLM configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY (or use --dry-run).")
    players = [(u, False) for u in args.live] if args.live else [(u, True) for u in service.fixtures.usernames()]
    rows = []
    try:
        for username, demo in players:
            session = _analyse(service, username, args.games, demo)
            for rep in range(args.repeats):
                print(f"[{username} #{rep + 1}] running conditions...", flush=True)
                if args.dry_run:
                    # Scripted outputs: a plausible-looking ungrounded answer with typical errors, and the template.
                    provider = ScriptedProvider(
                        [
                            LLMResponse(text="In [[G1 22...Qd7]] you should have played Qh5, reaching +3.2. Also [[G7 10.Nf3]] was weak."),
                            LLMResponse(text=template_report(session)),
                        ]
                    )
                # 1) ungrounded baseline: move lists only, no tools
                base_ctx = build_baseline_context(session)
                t0 = time.time()
                toolbox = Toolbox(session, service.pool, settings)
                res = run_agent(
                    provider,
                    prompts.BASELINE_SYSTEM + "\n\n" + prompts.context_message(base_ctx),
                    [{"role": "user", "text": BASELINE_TASK}],
                    toolbox,
                    0,
                    use_tools=False,
                )
                base = _metrics(
                    verify(res.text, session, base_ctx), res.text, time.time() - t0, {"input_tokens": res.input_tokens, "output_tokens": res.output_tokens}
                )
                # 2) + 3) grounded, first draft and after verify-then-repair
                system = prompts.REPORT_SYSTEM + "\n\n" + prompts.context_message(build_context(session))
                t0 = time.time()
                out = _respond(session, provider, service.pool, settings, system, [{"role": "user", "text": prompts.REPORT_TASK}])
                first_v = out["verification_before_repair"] or out["verification"]
                grounded = _metrics(first_v, out["markdown"], out["latency_s"], out["usage"])
                grounded["tool_calls"] = len(out["trace"])
                repaired = _metrics(out["verification"], out["markdown"], time.time() - t0, out["usage"])
                repaired["repair_triggered"] = out["repaired"]
                rows.append({"player": username, "games": len(session.games), "run": rep + 1, "ungrounded": base, "grounded": grounded, "grounded_repair": repaired})
                if not args.dry_run:
                    (OUT / "samples").mkdir(exist_ok=True)
                    (OUT / "samples" / f"{username}-{rep + 1}-grounded.md").write_text(out["markdown"])
                    (OUT / "samples" / f"{username}-{rep + 1}-ungrounded.md").write_text(res.text)
    finally:
        service.close()
    return {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": getattr(provider, "name", None),
        "model": getattr(provider, "model", None),
        "dry_run": args.dry_run,
        "rows": rows,
    }


def summarize(result: dict) -> str:
    keys = ["grounding_score", "citation_validity", "move_legality", "recommendation_agreement", "eval_support", "citations", "words", "latency_s", "tokens"]
    conds = [("ungrounded", "Ungrounded (PGN only)"), ("grounded", "Grounded, first draft"), ("grounded_repair", "Grounded + verify/repair")]

    def avg(cond, k):
        vals = [r[cond][k] for r in result["rows"] if r[cond].get(k) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    lines = [
        f"# Eval results ({result['model']}, {result['date']})",
        "",
        f"{len(result['rows'])} run(s) over: " + ", ".join(sorted({f"{r['player']} ({r['games']} games)" for r in result["rows"]})),
        "",
        "| Metric | " + " | ".join(c[1] for c in conds) + " |",
        "|---|" + "---|" * len(conds),
    ]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(avg(c, k)) for c, _ in conds) + " |")
    lines += ["", "Flagged items (first run per player):", ""]
    for r in result["rows"]:
        if r["run"] == 1:
            for c, label in conds:
                lines.append(f"- {r['player']} / {label}: {', '.join(r[c]['flagged']) or 'none'}")
    lines += ["", "Rates are passed/total for each check type; None means the answer made no claims of that type.", "Raw outputs are in evals/samples/."]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", nargs="*", default=None, help="Chess.com usernames to fetch live (default: offline fixtures)")
    ap.add_argument("--games", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--provider", default=None, help="anthropic | openai (default: auto from env)")
    ap.add_argument("--dry-run", action="store_true", help="no API calls; checks the harness end to end")
    args = ap.parse_args()
    result = run(args)
    md = summarize(result)
    print("\n" + md)
    if args.dry_run:
        print("\nDRY RUN: scripted outputs, numbers are not results. Nothing was written.")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (OUT / "results").mkdir(exist_ok=True)
    (OUT / "results" / f"{stamp}.json").write_text(json.dumps(result, indent=2))
    (OUT / "RESULTS.md").write_text(md + "\n")
    print(f"\nWrote evals/RESULTS.md and evals/results/{stamp}.json")


if __name__ == "__main__":
    main()
