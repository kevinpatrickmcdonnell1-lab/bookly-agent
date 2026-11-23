"""Print the numbers the README and docs/EVALS.md cite, straight from evals/results/*.json.

    uv run python scripts/eval_report.py

"New runs" are the latest full (--set all, 31-scenario) run per executor preset that recorded a git commit.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from evals.run_evals import pct  # noqa: E402  - same percentile definition as the harness

RESULTS = ROOT / "evals" / "results"
PRESETS = ("haiku", "sonnet", "hybrid")
STALL_MS = 60_000


def load_all() -> list[tuple[Path, dict]]:
    return [(p, json.loads(p.read_text())) for p in sorted(RESULTS.glob("*.json"))]


def preset_of(path: Path, summary: dict) -> str:
    return summary.get("executor_preset") or path.stem.rsplit("-", 1)[-1]


def turns(runs: list[dict]):
    for r in runs:
        for i, ms in enumerate(r["turn_latency_ms"], start=1):
            yield ms, r["scenario"], r["run"], i


def guard_router_ms(runs: list[dict]) -> tuple[float, int]:
    """Mean over turns of max(input_guard, route) latency; they run in parallel."""
    per_turn = []
    for r in runs:
        by_turn: dict[int, list[int]] = {}
        for e in r["events"]:
            if e["type"] in ("input_guard", "route"):
                by_turn.setdefault(e["turn"], []).append(e["latency_ms"] or 0)
        per_turn += [max(v) for v in by_turn.values()]
    return (statistics.mean(per_turn) if per_turn else 0.0), len(per_turn)


def staff_pick_blocks(runs: list[dict]) -> tuple[int, int, int]:
    """focus_recommended_book conversations: (total, supervisor blocked a claim, block led to a failed run)."""
    convs = [r for r in runs if r["scenario"] == "focus_recommended_book"]
    blocked = [r for r in convs if any(
        e["type"] == "output_check" and not e["payload"].get("ok", True)
        and any("unsupported policy claim" in str(i) for i in e["payload"].get("issues", []))
        for e in r["events"])]
    return len(convs), len(blocked), sum(not r["passed"] for r in blocked)


def main() -> None:
    sets = {s["id"]: s["set"] for s in yaml.safe_load((ROOT / "evals" / "scenarios.yaml").read_text())}
    files = load_all()

    latest: dict[str, tuple[Path, dict]] = {}
    for path, d in files:
        s = d["summary"]
        if s.get("git_commit") and s["label"].startswith("all-") and s["scenario_count"] == 31:
            latest[preset_of(path, s)] = (path, d)  # files sort by timestamp, so the last one wins

    for preset in PRESETS:
        if preset not in latest:
            print(f"\n## {preset}: no full run with a recorded git commit\n")
            continue
        path, d = latest[preset]
        s, runs = d["summary"], d["runs"]
        print(f"\n## {preset}  ({path.name})")
        print(f"commit {s['git_commit']}  dirty={s['git_dirty']}  executor_preset={s['executor_preset']}")
        print(f"passed: {sum(r['passed'] for r in runs)}/{len(runs)}")
        for name in ("core", "extended", "hard"):
            rs = [r for r in runs if sets.get(r["scenario"]) == name]
            print(f"  {name}: {sum(r['passed'] for r in rs)}/{len(rs)}")
        lat = [ms for ms, *_ in turns(runs)]
        print(f"turn latency p50 / p95: {pct(lat, 0.5) / 1000:.1f} s / {pct(lat, 0.95) / 1000:.1f} s"
              f"  ({len(lat)} turns)")
        stalls = [t for t in turns(runs) if t[0] > STALL_MS]
        print(f"turns over {STALL_MS // 1000} s: {len(stalls)}"
              + ("".join(f"\n  {ms / 1000:.1f} s  {sc} run {rn} turn {tn}" for ms, sc, rn, tn in stalls)))
        print(f"mean cost per conversation: ${statistics.mean(r['cost_usd'] for r in runs):.4f}")
        print(f"refund leakage: ${sum(r['leakage_cents'] for r in runs) / 100:.2f}")
        failed = [r for r in runs if not r["passed"]]
        print(f"failed runs: {len(failed)}")
        for r in failed:
            print(f"  {r['scenario']} run {r['run']}: {'; '.join(r['failures'])}")
        print("three slowest turns:")
        for ms, sc, rn, tn in sorted(turns(runs), reverse=True)[:3]:
            print(f"  {ms / 1000:.1f} s  {sc} run {rn} turn {tn}")
        mean_gr, n_gr = guard_router_ms(runs)
        print(f"guard+router, mean of max(input_guard, route) per turn: {mean_gr / 1000:.2f} s  ({n_gr} turns)")

    print("\n## All saved runs")
    print(f"runs: {len(files)}")
    print(f"conversations: {sum(len(d['runs']) for _, d in files):,}")
    print(f"refund leakage: ${sum(r['leakage_cents'] for _, d in files for r in d['runs']) / 100:.2f}")

    print("\n## True 'staff pick' claim blocked by the supervisor (focus_recommended_book, 31-scenario runs)")
    tot = [0, 0, 0]
    for path, d in files:
        if d["summary"]["scenario_count"] != 31:
            continue
        n, blocked, failed = staff_pick_blocks(d["runs"])
        tot = [tot[0] + n, tot[1] + blocked, tot[2] + failed]
        print(f"  {path.name}: blocked in {blocked}/{n}, run failed in {failed}/{n}")
    print(f"  total: blocked in {tot[1]}/{tot[0]}, run failed in {tot[2]}/{tot[0]}")


if __name__ == "__main__":
    main()
