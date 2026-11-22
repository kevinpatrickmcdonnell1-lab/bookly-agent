"""The eval harness itself is code, so it gets no-key tests too."""

import asyncio

from bookly.agent.state import Session
from bookly.config import Settings
from bookly.tools.knowledge import PolicyIndex
from evals.run_evals import check, leakage_cents, load, run_one
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
