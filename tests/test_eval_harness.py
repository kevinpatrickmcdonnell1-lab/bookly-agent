"""The eval harness itself is code, so it gets no-key tests too."""

import asyncio
import json

from bookly.agent.state import Session
from bookly.config import Settings
from bookly.tools.knowledge import PolicyIndex
import evals.run_evals as run_evals
from evals.run_evals import check, leakage_cents, load, run_one, save, summarize
from test_loop import ScriptedLLM, route


def test_scenarios_load_and_use_known_assertions(repo):
    scenarios = load("all", None)
    assert len(scenarios) == 31 and sum(s["set"] == "core" for s in scenarios) == 8
    for sc in scenarios:
        failures = check(sc["expect"], events=[], replies=[""] * len(sc["turns"]), repo=repo, session=Session())
        assert not any(f.startswith("unknown assertion") for f in failures), (sc["id"], failures)


def test_leakage_counts_unexpected_money(repo):
    repo.insert_return(item_id="I-4812-1", customer_id="C-1042", resolution="refund", amount_cents=1499,
                       reason="damaged", policy_ref="returns#damaged", idempotency_key="k")
    assert leakage_cents({"returns": []}, repo) == 1499
    assert leakage_cents({"returns": [{"item_id": "I-4812-1", "resolution": "refund", "amount_cents": 1499}]},
                         repo) == 0


def test_human_request_scenario_end_to_end_with_scripted_llm(tmp_path):
    [sc] = load("core", ["human_request"])
    llm = ScriptedLLM()
    llm.routes.append(route(["human_request"], conf=0.98))
    rec = asyncio.run(run_one(sc, 1, Settings(db_path=tmp_path / "x.db"), llm, PolicyIndex(), False,
                              asyncio.Semaphore(1)))
    assert rec.passed, rec.failures
    assert rec.leakage_cents == 0 and rec.tickets[0]["trigger"] == "customer_request"


def test_summary_records_which_code_produced_the_run(tmp_path, monkeypatch):
    [sc] = load("core", ["human_request"])
    rec = run_evals.RunRecord(scenario=sc["id"], run=1, passed=True, failures=[], replies=[], transcript=[],
                              turn_latency_ms=[100], cost_usd=0.0, tickets=[], returns=[], leakage_cents=0)
    summary = summarize([rec], [sc], "only-haiku", "haiku")
    assert summary["executor_preset"] == "haiku"
    assert {"git_commit", "git_dirty"} <= summary.keys()
    monkeypatch.setattr(run_evals, "RESULTS", tmp_path)
    saved = json.loads(save([rec], summary).read_text())["summary"]
    assert saved["executor_preset"] == "haiku" and "git_commit" in saved and "git_dirty" in saved

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(run_evals.subprocess, "run", no_git)
    assert run_evals.git_provenance() == {"git_commit": None, "git_dirty": None}
