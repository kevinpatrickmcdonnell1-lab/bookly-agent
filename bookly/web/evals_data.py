"""Read saved eval runs (evals/results/*.json) for the dashboard tab.

Pure file reading, no API key. Results are gitignored, so the dashboard shows
whatever has been run on this machine. Files are parsed once per mtime.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from bookly.config import ROOT

RESULTS = ROOT / "evals" / "results"
SCENARIOS = ROOT / "evals" / "scenarios.yaml"
_NAME = re.compile(r"^(\d{8}-\d{6})-(.+)\.json$")
_cache: dict[Path, tuple[float, dict]] = {}


def _scenario_sets() -> dict[str, str]:
    """Scenario id -> set (core / extended / hard), from the current scenarios.yaml."""
    try:
        scenarios = yaml.safe_load(SCENARIOS.read_text()) or []
    except (OSError, yaml.YAMLError):
        return {}
    return {sc["id"]: sc.get("set", "?") for sc in scenarios if "id" in sc}


def _suite_sizes(sets: dict[str, str]) -> dict[str, int]:
    return {**Counter(sets.values()), "all": len(sets)} if sets else {}


def _load(path: Path) -> dict:
    mtime = path.stat().st_mtime
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    data = json.loads(path.read_text())
    _cache[path] = (mtime, data)
    return data


def _models(runs: list[dict], event_type: str) -> list[str]:
    seen = Counter(e["model"] for r in runs for e in r.get("events", [])
                   if e.get("type") == event_type and e.get("model"))
    return [m for m, _ in seen.most_common()]


def _by_set(runs: list[dict], sets: dict[str, str]) -> dict[str, dict]:
    """Pass counts per scenario set, so an all-suite run can be read set by set."""
    out: dict[str, dict] = {}
    for r in runs:
        b = out.setdefault(sets.get(r["scenario"], "?"), {"passed": 0, "conversations": 0})
        b["passed"] += bool(r["passed"])
        b["conversations"] += 1
    return out


def _row(path: Path, data: dict, sets: dict[str, str]) -> dict:
    sizes = _suite_sizes(sets)
    m = _NAME.match(path.name)
    stamp = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S") if m else datetime.fromtimestamp(path.stat().st_mtime)
    s, runs = data["summary"], data["runs"]
    label = s.get("label", m.group(2) if m else path.stem)
    suite, _, preset = label.partition("-")
    full = sizes.get(suite)
    runs_per = max((v["runs"] for v in s["scenarios"].values()), default=0)
    return {
        "id": path.stem,
        "started": stamp.isoformat(timespec="seconds"),
        "label": label,
        "suite": suite,
        "executor": preset or "?",
        "suite_size": full,
        # Fewer scenarios than the suite has now: a full run from before scenarios were
        # added, or a spot check (under half the suite, or an --only run).
        "partial": suite == "only" or (full is not None and s["scenario_count"] < full),
        "spot_check": suite == "only" or (full is not None and s["scenario_count"] * 2 < full),
        "scenario_count": s["scenario_count"],
        "runs_per_scenario": runs_per,
        "conversations": len(runs),
        "passed": sum(r["passed"] for r in runs),
        "pass_rate": s["pass_rate"],
        "all_runs_pass": s["all_runs_pass"],
        "refund_leakage_cents": s["refund_leakage_cents"],
        "turn_latency_ms": s["turn_latency_ms"],
        "cost_per_conversation_usd": s["cost_per_conversation_usd"],
        "conversations_escalated": s.get("conversations_escalated", 0),
        "escalations_by_trigger": s.get("escalations_by_trigger", {}),
        "tone_rubric": s.get("tone_rubric", {}),
        "executor_models": _models(runs, "executor"),
        "by_set": _by_set(runs, sets),
    }


def list_runs(results: Path = RESULTS) -> list[dict]:
    """One summary row per saved run, newest first. Unreadable files are skipped."""
    sets = _scenario_sets()
    rows = []
    for path in sorted(results.glob("*.json")) if results.is_dir() else []:
        try:
            rows.append(_row(path, _load(path), sets))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    rows.sort(key=lambda r: r["started"], reverse=True)
    return rows


def run_detail(run_id: str, results: Path = RESULTS) -> dict | None:
    """Per-scenario results and per-conversation transcripts for one run (trace events left out)."""
    if not re.fullmatch(r"[\w.-]+", run_id):
        return None
    path = results / f"{run_id}.json"
    if not path.is_file():
        return None
    data = _load(path)
    convs = [{
        "scenario": r["scenario"], "run": r["run"], "passed": r["passed"], "failures": r["failures"],
        "transcript": r.get("transcript", []), "turn_latency_ms": r.get("turn_latency_ms", []),
        "cost_usd": r.get("cost_usd", 0), "leakage_cents": r.get("leakage_cents", 0),
        "tickets": [{k: t.get(k) for k in ("queue", "trigger", "priority")} for t in r.get("tickets", [])],
        "returns": [{k: x.get(k) for k in ("item_id", "resolution", "amount_cents")} for x in r.get("returns", [])],
        "judge": r.get("judge"), "error": r.get("error"),
    } for r in data["runs"]]
    convs.sort(key=lambda c: (c["scenario"], c["run"]))
    return {"run": _row(path, data, _scenario_sets()), "scenarios": data["summary"]["scenarios"],
            "conversations": convs}
