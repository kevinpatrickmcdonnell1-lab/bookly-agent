"""Run the same eval suite with each executor setup and print the comparison table.

    python -m evals.compare_models --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace

from bookly.config import get_settings
from bookly.runtime import require_key
from evals.run_evals import load, print_report, run_suite, save, summarize

SETUPS = [
    ("Haiku for all routes", "haiku"),
    ("Sonnet for all routes", "sonnet"),
    ("Hybrid: Haiku except returns on Sonnet", "hybrid"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="core", choices=["core", "extended", "all"])
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    require_key()
    base = get_settings()
    scenarios = load(args.set, None)
    rows = []
    for name, preset in SETUPS:
        settings = replace(base, executor_preset=preset, executor_model_override="")
        records = asyncio.run(run_suite(scenarios, args.runs, settings))
        summary = summarize(records, scenarios, f"{args.set}-{preset}")
        print_report(summary)
        save(records, summary)
        rows.append((name, summary))

    print("\n## Executor comparison\n")
    print("| Executor setup | Pass rate | Scenarios 3/3 | p50 / p95 turn latency | Cost per conversation | Leakage |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, s in rows:
        print(f"| {name} | {s['pass_rate']:.0%} | {s['all_runs_pass']}/{s['scenario_count']} | "
              f"{s['turn_latency_ms']['p50']} / {s['turn_latency_ms']['p95']} ms | "
              f"${s['cost_per_conversation_usd']:.4f} | ${s['refund_leakage_cents'] / 100:.2f} |")


if __name__ == "__main__":
    main()
