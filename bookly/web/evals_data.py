"""Read saved eval runs (evals/results/*.json) for the dashboard tab.

Pure file reading, no API key. Every run is committed to evals/results/, and runs
saved since provenance tracking record the git commit that produced them.
Files are parsed once per mtime.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from math import comb
from datetime import datetime
from pathlib import Path

import yaml

from bookly.config import ROOT

RESULTS = ROOT / "evals" / "results"
SCENARIOS = ROOT / "evals" / "scenarios.yaml"
_NAME = re.compile(r"^(\d{8}-\d{6})-(.+)\.json$")
_cache: dict[Path, tuple[float, dict]] = {}


def _scenarios() -> list[dict]:
    try:
        return [sc for sc in (yaml.safe_load(SCENARIOS.read_text()) or []) if "id" in sc]
    except (OSError, yaml.YAMLError):
        return []


def _scenario_sets() -> dict[str, str]:
    """Scenario id -> set (core / extended / hard), from the current scenarios.yaml."""
    return {sc["id"]: sc.get("set", "?") for sc in _scenarios()}


def _money(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _words(xs) -> str:
    return ", ".join(f'"{x}"' if isinstance(x, str) else str(x) for x in xs)


def _turn(t) -> str:
    return "the last turn" if t == "last" else f"turn {t}"


def describe(expect: dict) -> list[str]:
    """Plain-language list of what a scenario's assertions check."""
    out: list[str] = []
    for key, want in expect.items():
        if key == "tool_called":
            out.append("Calls " + ", ".join(want))
        elif key == "tool_not_called":
            out.append("Never calls " + ", ".join(want))
        elif key == "tool_not_called_until":
            out.append(f"Doesn't call {', '.join(want['tools'])} before turn {want['turn'] + 1}")
        elif key == "tool_first_called_in":
            out += [f"First calls {t} in turn {n}" for t, n in want.items()]
        elif key == "max_tool_calls":
            out += [f"Calls {t} at most {n} time{'s' * (n != 1)}" for t, n in want.items()]
        elif key == "reply_is_question":
            out.append("Asks a question in " + ", ".join(f"turn {t}" for t in want))
        elif key == "reply_contains":
            out += [f"Reply in {_turn(t)} mentions {_words(w)}" for t, w in want.items()]
        elif key == "reply_contains_any":
            out += [f"Reply in {_turn(t)} mentions one of {_words(w)}" for t, w in want.items()]
        elif key == "reply_not_contains":
            out.append(f"No reply says {_words(want)}")
        elif key == "reply_contains_tracking":
            out.append(f"Gives the real tracking number for order {want}")
        elif key == "returns":
            out.append("No return or refund is created" if not want else "Creates exactly: " + "; ".join(
                f"{r['resolution'].replace('_', ' ')} of {_money(r['amount_cents'])} for {r['item_id']}" for r in want))
        elif key == "no_refund_for":
            out.append("No cash refund for " + ", ".join(want))
        elif key == "tickets":
            bits = []
            if "count" in want:
                bits.append("no escalation ticket" if want["count"] == 0
                            else f"{want['count']} escalation ticket{'s' * (want['count'] != 1)}")
            bits += [f"{f} {want[f]}" for f in ("queue", "trigger") if f in want]
            out.append("Ticket: " + ", ".join(bits) if bits else "Escalation ticket")
        elif key == "escalated_in_turn":
            out.append(f"Hands off to a human in turn {want}")
        elif key == "no_executor_in_turn":
            out.append(f"Turn {want} is handled by code alone, without the executor model")
        elif key in ("event", "event_absent"):
            payload = ", ".join(f"{k}={v}" for k, v in (want.get("payload") or {}).items())
            where = f" in turn {want['turn']}" if "turn" in want else ""
            art = "No" if key == "event_absent" else ("An" if want["type"][0] in "aeiou" else "A")
            out.append(f"{art} {want['type']} trace event{where}"
                       + (f" with {payload}" if payload else ""))
        elif key == "policy_ref_seen":
            out.append(f"Consults policy section {want}")
        elif key == "password_reset_to":
            out.append(f"Sends exactly one password reset, to {want}")
        else:
            out.append(key)
    return out


def catalog() -> dict:
    """Scenario sets and, per scenario, its opening message, turn count and plain-language checks."""
    scenarios = _scenarios()
    return {
        "sets": dict(Counter(sc.get("set", "?") for sc in scenarios)),
        "scenarios": {sc["id"]: {"set": sc.get("set", "?"), "turns": len(sc.get("turns", [])),
                                 "opening": (sc.get("turns") or [""])[0], "checks": describe(sc.get("expect", {}))}
                      for sc in scenarios},
    }


def failure_upper95(failures: int, n: int) -> float:
    """Clopper-Pearson 95% upper bound on the true failure rate, given failures seen in n conversations."""
    if n <= 0:
        return 1.0
    if failures >= n:
        return 1.0
    lo, hi = failures / n, 1.0
    for _ in range(60):
        p = (lo + hi) / 2
        cdf = sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(failures + 1))
        lo, hi = (p, hi) if cdf > 0.025 else (lo, p)
    return round(hi, 4)


def _failure_cause(r: dict) -> str | None:
    """Why a conversation failed: a crash, the output supervisor rejecting a claim, or another assertion."""
    if r["passed"]:
        return None
    if r.get("error"):
        return "crash"
    for e in r.get("events", []):
        if (e.get("type") == "output_check" and not (e.get("payload") or {}).get("ok", True)
                and any("unsupported policy claim" in str(i) for i in (e.get("payload") or {}).get("issues", []))):
            return "supervisor"
    return "assertion"


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
        # From the raw per-conversation costs: the summary's value is already rounded to 5 places.
        "cost_per_conversation_usd": (sum(r.get("cost_usd") or 0 for r in runs) / len(runs)) if runs
        else s["cost_per_conversation_usd"],
        "conversations_escalated": s.get("conversations_escalated", 0),
        "escalations_by_trigger": s.get("escalations_by_trigger", {}),
        "tone_rubric": s.get("tone_rubric", {}),
        "git_commit": s.get("git_commit"),
        "git_dirty": s.get("git_dirty"),
        "failure_causes": dict(Counter(c for c in map(_failure_cause, runs) if c)),
        "failure_upper95": failure_upper95(len(runs) - sum(bool(r["passed"]) for r in runs), len(runs)),
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
        "judge": r.get("judge"), "error": r.get("error"), "cause": _failure_cause(r),
    } for r in data["runs"]]
    convs.sort(key=lambda c: (c["scenario"], c["run"]))
    cat = catalog()["scenarios"]
    return {"run": _row(path, data, _scenario_sets()), "scenarios": data["summary"]["scenarios"],
            "conversations": convs, "catalog": {sid: cat[sid] for sid in data["summary"]["scenarios"] if sid in cat}}
