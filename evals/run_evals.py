"""Scenario evals: scripted multi-turn conversations against the real models.

    python -m evals.run_evals --set core --runs 3
    python -m evals.run_evals --set all --runs 3 --judge     # + Opus tone rubric
    python -m evals.run_evals --only return_staff_pick --runs 1 --show

Each scenario runs N times on a fresh database, because model output varies.
Assertions check outcomes (database rows, tool calls, trace events), never wording.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from bookly.agent.loop import Agent
from bookly.agent.state import Session
from bookly.config import ROOT, Settings, get_settings
from bookly.data.repo import Repo
from bookly.runtime import require_key
from bookly.tools.knowledge import PolicyIndex

SCENARIOS = Path(__file__).with_name("scenarios.yaml")
RESULTS = Path(__file__).with_name("results")
CORE_IDS = None  # filled from the yaml


@dataclass
class RunRecord:
    scenario: str
    run: int
    passed: bool
    failures: list[str]
    replies: list[str]
    transcript: list[dict]
    turn_latency_ms: list[int]
    cost_usd: float
    tickets: list[dict]
    returns: list[dict]
    leakage_cents: int
    error: str | None = None
    judge: dict | None = None
    events: list[dict] = field(default_factory=list)


# ---- assertions -------------------------------------------------------------------

def _ok_calls(events, tool=None):
    return [e for e in events if e["type"] == "tool_call" and (tool is None or e["payload"]["tool"] == tool)]


def _match(payload: Any, want: Any) -> bool:
    if isinstance(want, dict):
        return isinstance(payload, dict) and all(_match(payload.get(k), v) for k, v in want.items())
    return payload == want


def _turn(key, n_turns):
    return n_turns if key == "last" else int(key)


def check(expect: dict, *, events: list[dict], replies: list[str], repo: Repo, session: Session) -> list[str]:
    f: list[str] = []
    n = len(replies)
    lower = [r.lower() for r in replies]
    for key, want in expect.items():
        if key == "tool_called":
            for t in want:
                if not _ok_calls(events, t):
                    f.append(f"{t} was never called successfully")
        elif key == "tool_not_called":
            for t in want:
                if _ok_calls(events, t):
                    f.append(f"{t} was called but shouldn't have been")
        elif key == "tool_not_called_until":
            for t in want["tools"]:
                early = [e for e in _ok_calls(events, t) if e["turn"] <= want["turn"]]
                if early:
                    f.append(f"{t} ran in turn {early[0]['turn']} (must wait until after turn {want['turn']})")
        elif key == "tool_first_called_in":
            for t, turn in want.items():
                calls = _ok_calls(events, t)
                if not calls:
                    f.append(f"{t} never ran (expected in turn {turn})")
                elif calls[0]["turn"] != turn:
                    f.append(f"{t} first ran in turn {calls[0]['turn']}, expected {turn}")
        elif key == "max_tool_calls":
            for t, mx in want.items():
                if len(_ok_calls(events, t)) > mx:
                    f.append(f"{t} ran {len(_ok_calls(events, t))} times (max {mx})")
        elif key == "reply_is_question":
            for t in want:
                if t > n or "?" not in replies[t - 1]:
                    f.append(f"turn {t} reply isn't a question")
        elif key == "reply_contains":
            for t, words in want.items():
                t = _turn(t, n)
                for w in words:
                    if t > n or w.lower() not in lower[t - 1]:
                        f.append(f"turn {t} reply doesn't mention {w!r}")
        elif key == "reply_contains_any":
            for t, words in want.items():
                t = _turn(t, n)
                if t > n or not any(w.lower() in lower[t - 1] for w in words):
                    f.append(f"turn {t} reply mentions none of {words}")
        elif key == "reply_not_contains":
            for w in want:
                if any(w.lower() in r for r in lower):
                    f.append(f"a reply contains {w!r}")
        elif key == "reply_contains_tracking":
            tracking = repo.order(want)["tracking_number"]
            if not any(tracking in r for r in replies):
                f.append(f"no reply contains tracking number {tracking}")
        elif key == "returns":
            got = sorted((r["item_id"], r["resolution"], r["amount_cents"]) for r in repo.returns())
            exp = sorted((r["item_id"], r["resolution"], r["amount_cents"]) for r in want)
            if got != exp:
                f.append(f"returns {got} != expected {exp}")
        elif key == "no_refund_for":
            for item in want:
                if any(r["item_id"] == item and r["resolution"] == "refund" for r in repo.returns()):
                    f.append(f"cash refund issued for {item}")
        elif key == "tickets":
            tickets = repo.tickets()
            if "count" in want and len(tickets) != want["count"]:
                f.append(f"{len(tickets)} tickets, expected {want['count']}")
            for fld in ("queue", "trigger"):
                if fld in want and not any(t[fld] == want[fld] for t in tickets):
                    f.append(f"no ticket with {fld}={want[fld]} (got {[t[fld] for t in tickets]})")
        elif key == "escalated_in_turn":
            if not any(e["type"] == "escalation" and e["turn"] == want for e in events):
                f.append(f"no escalation in turn {want}")
        elif key == "no_executor_in_turn":
            if any(e["type"] == "executor" and e["turn"] == want for e in events):
                f.append(f"executor model was called in turn {want}")
        elif key == "event":
            if not any(e["type"] == want["type"] and e["turn"] == want.get("turn", e["turn"])
                       and _match(e["payload"], want.get("payload", {})) for e in events):
                f.append(f"no {want['type']} event matching {want.get('payload')}")
        elif key == "event_absent":
            if any(e["type"] == want["type"] and _match(e["payload"], want.get("payload", {})) for e in events):
                f.append(f"unexpected {want['type']} event matching {want.get('payload')}")
        elif key == "policy_ref_seen":
            if want not in session.policy_refs and want not in session.policy_texts:
                f.append(f"policy {want} was never consulted")
        elif key == "password_reset_to":
            resets = repo.password_resets()
            if [r["sent_to"] for r in resets] != [want]:
                f.append(f"password resets {[r['sent_to'] for r in resets]} != [{want}]")
        else:
            f.append(f"unknown assertion {key}")
    return f


def leakage_cents(expect: dict, repo: Repo) -> int:
    """Money that moved without being expected: the number that must stay at 0."""
    rows = repo.returns()
    allowed = {(r["item_id"], r["resolution"], r["amount_cents"]) for r in expect.get("returns", [])}
    leak = 0
    for r in rows:
        item = repo.item(r["item_id"])
        wrong_amount = item is not None and r["amount_cents"] > item["price_cents"]
        unexpected = "returns" in expect and (r["item_id"], r["resolution"], r["amount_cents"]) not in allowed
        forbidden = r["item_id"] in expect.get("no_refund_for", []) and r["resolution"] == "refund"
        if wrong_amount or unexpected or forbidden:
            leak += r["amount_cents"]
    return leak


# ---- judge (offline, Opus) ------------------------------------------------------------

JUDGE_SYSTEM = """You grade one customer-support transcript from Bookly, an independent online bookstore whose support should sound like a warm, knowledgeable bookseller. Score each criterion 1 (poor) to 5 (excellent):
- warmth: kind, human, not robotic or pushy
- concision: short replies, one question at a time, no padding
- plain_text: no markdown, lists or links; would sound natural read aloud
- no_invented_facts: never states facts, policies or promises the conversation doesn't support
- recommendation_restraint: at most one book suggestion, never when the customer is upset (5 if none was appropriate and none was made)
Return JSON only, with a one-sentence note."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "integer"} for k in ("warmth", "concision", "plain_text", "no_invented_facts",
                                                    "recommendation_restraint")} | {"note": {"type": "string"}},
    "required": ["warmth", "concision", "plain_text", "no_invented_facts", "recommendation_restraint", "note"],
    "additionalProperties": False,
}


async def judge(llm, model: str, transcript: list[dict]) -> dict:
    text = "\n".join(f"{m['who'].upper()}: {m['text']}" for m in transcript)
    data, call = await llm.structured(model=model, system=JUDGE_SYSTEM, user=f"<transcript>\n{text}\n</transcript>",
                                      schema=JUDGE_SCHEMA, max_tokens=4000, effort="medium")
    data["cost_usd"] = call.cost_usd
    return data


# ---- runner ---------------------------------------------------------------------------

async def run_one(sc: dict, run: int, settings: Settings, llm, kb, do_judge: bool, sem: asyncio.Semaphore) -> RunRecord:
    async with sem:
        tmp = Path(tempfile.mkdtemp(prefix="bookly-eval-")) / "eval.db"
        repo = Repo(tmp)
        repo.reset()
        agent = Agent(replace(settings, db_path=tmp), repo, llm, kb)
        session = Session()
        replies, transcript, latencies, events = [], [], [], []
        error = None
        try:
            for text in sc["turns"]:
                t0 = time.perf_counter()
                result = await agent.run_turn(session, text)
                latencies.append(int((time.perf_counter() - t0) * 1000))
                replies.append(result.reply)
                events.extend(result.events)
                transcript += [{"who": "customer", "text": text}, {"who": "bookly", "text": result.reply}]
        except Exception as exc:  # noqa: BLE001 - record and keep going
            error = repr(exc)
        failures = [f"crashed: {error}"] if error else []
        if not error:
            failures += check(sc["expect"], events=events, replies=replies, repo=repo, session=session)
        rec = RunRecord(
            scenario=sc["id"], run=run, passed=not failures, failures=failures, replies=replies,
            transcript=transcript, turn_latency_ms=latencies,
            cost_usd=sum(e["cost_usd"] or 0 for e in events), tickets=repo.tickets(), returns=repo.returns(),
            leakage_cents=leakage_cents(sc["expect"], repo), error=error, events=events,
        )
        if do_judge and not error:
            try:
                rec.judge = await judge(llm, settings.judge_model, transcript)
            except Exception as exc:  # noqa: BLE001
                rec.judge = {"error": repr(exc)}
        return rec


def load(which: str, only: list[str] | None) -> list[dict]:
    scenarios = yaml.safe_load(SCENARIOS.read_text())
    if only:
        return [s for s in scenarios if s["id"] in only]
    return [s for s in scenarios if which == "all" or s["set"] == which]


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(q * (len(values) - 1))))
    return values[k]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def git_provenance() -> dict:
    """Which code produced a run. None when git isn't available, rather than failing the run."""
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    dirty = None
    if status is not None:
        paths = [line[3:].split(" -> ")[-1].strip('"') for line in status.splitlines() if line.strip()]
        dirty = any(not p.startswith("evals/results/") for p in paths)
    return {"git_commit": head.strip() if head else None, "git_dirty": dirty}


def summarize(records: list[RunRecord], scenarios: list[dict], label: str,
              executor_preset: str | None = None) -> dict:
    by = {s["id"]: [r for r in records if r.scenario == s["id"]] for s in scenarios}
    turn_lat = [l for r in records for l in r.turn_latency_ms]
    triggers: dict[str, int] = {}
    for r in records:
        for t in r.tickets:
            triggers[t["trigger"]] = triggers.get(t["trigger"], 0) + 1
    judged = [r.judge for r in records if r.judge and "error" not in r.judge]
    rubric = {}
    if judged:
        for k in ("warmth", "concision", "plain_text", "no_invented_facts", "recommendation_restraint"):
            rubric[k] = round(statistics.mean(j[k] for j in judged), 2)
    return {
        "label": label,
        **git_provenance(),
        "executor_preset": executor_preset,
        "scenarios": {sid: {"passed": sum(r.passed for r in rs), "runs": len(rs),
                            "failures": sorted({f for r in rs for f in r.failures})} for sid, rs in by.items()},
        "pass_rate": round(sum(r.passed for r in records) / max(1, len(records)), 3),
        "all_runs_pass": sum(all(r.passed for r in rs) for rs in by.values()),
        "scenario_count": len(by),
        "refund_leakage_cents": sum(r.leakage_cents for r in records),
        "escalations_by_trigger": triggers,
        "conversations_escalated": round(sum(bool(r.tickets) for r in records) / max(1, len(records)), 3),
        "turn_latency_ms": {"p50": pct(turn_lat, 0.5), "p95": pct(turn_lat, 0.95)},
        "cost_per_conversation_usd": round(statistics.mean(r.cost_usd for r in records), 5) if records else 0,
        "tone_rubric": rubric,
    }


def print_report(summary: dict) -> None:
    print(f"\n## Eval report: {summary['label']}\n")
    print("| Scenario | Passed | Failures |")
    print("| --- | --- | --- |")
    for sid, s in summary["scenarios"].items():
        fails = "; ".join(s["failures"])[:140] or ""
        print(f"| {sid} | {s['passed']}/{s['runs']} | {fails} |")
    print()
    print(f"- Pass rate (all runs): {summary['pass_rate']:.0%}")
    print(f"- Scenarios passing every run: {summary['all_runs_pass']}/{summary['scenario_count']}")
    print(f"- Refund leakage: ${summary['refund_leakage_cents'] / 100:.2f}  (target $0.00)")
    print(f"- Escalations by trigger: {summary['escalations_by_trigger'] or 'none'}")
    print(f"- Turn latency p50 / p95: {summary['turn_latency_ms']['p50']} ms / {summary['turn_latency_ms']['p95']} ms")
    print(f"- Cost per conversation: ${summary['cost_per_conversation_usd']:.4f}")
    if summary["tone_rubric"]:
        print(f"- Tone rubric (Opus judge, 1-5): {summary['tone_rubric']}")


async def run_suite(scenarios: list[dict], runs: int, settings: Settings, do_judge: bool = False,
                    concurrency: int = 4) -> list[RunRecord]:
    from bookly.llm import AnthropicLLM
    llm = AnthropicLLM(fallbacks=settings.executor_fallbacks)
    kb = PolicyIndex()
    sem = asyncio.Semaphore(concurrency)
    jobs = [run_one(sc, i + 1, settings, llm, kb, do_judge, sem) for sc in scenarios for i in range(runs)]
    return list(await asyncio.gather(*jobs))


def save(records: list[RunRecord], summary: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}-{summary['label']}.json"
    path.write_text(json.dumps({"summary": summary, "runs": [r.__dict__ for r in records]}, indent=2, default=str))
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="core", choices=["core", "extended", "hard", "all"])
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--judge", action="store_true", help="grade transcripts with the Opus tone rubric")
    ap.add_argument("--show", action="store_true", help="print transcripts")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    require_key()
    settings = get_settings()
    scenarios = load(args.set, args.only)
    label = f"{'only' if args.only else args.set}-{settings.executor_preset}" + (f"-{settings.executor_model_override}"
                                                       if settings.executor_model_override else "")
    t0 = time.time()
    records = asyncio.run(run_suite(scenarios, args.runs, settings, args.judge, args.concurrency))
    if args.show:
        for r in records:
            print(f"\n--- {r.scenario} run {r.run}: {'PASS' if r.passed else 'FAIL ' + '; '.join(r.failures)}")
            for m in r.transcript:
                print(f"  {m['who']:>8}: {m['text']}")
    summary = summarize(records, scenarios, label, settings.executor_preset)
    print_report(summary)
    print(f"\nSaved {save(records, summary).relative_to(ROOT)} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
