"""The eval dashboard reads saved result files; no key, no network."""

import json

from bookly.web.evals_data import list_runs, run_detail


def _write(dir_, name, scenarios, runs):
    summary = {
        "label": name.split("-", 2)[2], "scenarios": scenarios,
        "pass_rate": sum(r["passed"] for r in runs) / len(runs),
        "all_runs_pass": sum(s["passed"] == s["runs"] for s in scenarios.values()),
        "scenario_count": len(scenarios), "refund_leakage_cents": 0, "escalations_by_trigger": {},
        "conversations_escalated": 0.0, "turn_latency_ms": {"p50": 4000, "p95": 8000},
        "cost_per_conversation_usd": 0.01, "tone_rubric": {},
    }
    (dir_ / f"{name}.json").write_text(json.dumps({"summary": summary, "runs": runs}))


def _run(sid, n, passed):
    return {"scenario": sid, "run": n, "passed": passed, "failures": [] if passed else ["x was never called"],
            "transcript": [{"who": "customer", "text": "hi"}, {"who": "bookly", "text": "hello"}],
            "turn_latency_ms": [4000], "cost_usd": 0.01, "tickets": [], "returns": [], "leakage_cents": 0,
            "events": [{"type": "executor", "model": "claude-sonnet-5-5"}]}


def test_lists_newest_first_marks_partial_and_skips_junk(tmp_path):
    _write(tmp_path, "20261122-100000-core-haiku", {"order_status_happy": {"passed": 1, "runs": 1, "failures": []}},
           [_run("order_status_happy", 1, True)])
    _write(tmp_path, "20261122-110000-core-sonnet",
           {"order_status_happy": {"passed": 0, "runs": 1, "failures": ["x was never called"]}},
           [_run("order_status_happy", 1, False)])
    (tmp_path / "20261122-120000-broken.json").write_text("{not json")
    rows = list_runs(tmp_path)
    assert [r["executor"] for r in rows] == ["sonnet", "haiku"]
    assert rows[0]["partial"] is True and rows[0]["passed"] == 0 and rows[0]["conversations"] == 1
    assert rows[0]["executor_models"] == ["claude-sonnet-5-5"]


def test_detail_drops_events_and_rejects_path_tricks(tmp_path):
    _write(tmp_path, "20261122-100000-core-haiku", {"order_status_happy": {"passed": 1, "runs": 1, "failures": []}},
           [_run("order_status_happy", 1, True)])
    d = run_detail("20261122-100000-core-haiku", tmp_path)
    assert d["conversations"][0]["transcript"][1]["text"] == "hello"
    assert "events" not in d["conversations"][0]
    assert run_detail("../secrets", tmp_path) is None
    assert run_detail("missing", tmp_path) is None


def test_breaks_pass_counts_down_by_scenario_set(tmp_path):
    # order_status_happy is core, amount_manipulation is hard, in the real scenarios.yaml.
    _write(tmp_path, "20261122-100000-all-haiku",
           {"order_status_happy": {"passed": 1, "runs": 1, "failures": []},
            "amount_manipulation": {"passed": 0, "runs": 1, "failures": ["x"]}},
           [_run("order_status_happy", 1, True), _run("amount_manipulation", 1, False)])
    [row] = list_runs(tmp_path)
    assert row["by_set"] == {"core": {"passed": 1, "conversations": 1}, "hard": {"passed": 0, "conversations": 1}}


def test_rows_carry_provenance_failure_causes_and_bounds(tmp_path):
    from bookly.web import evals_data
    blocked = _run("order_status_happy", 1, False)
    blocked["events"].append({"type": "output_check", "payload": {
        "ok": False, "issues": ["unsupported policy claim: it's a staff pick"]}})
    _write(tmp_path, "20261123-100000-all-haiku", {"order_status_happy": {"passed": 1, "runs": 3, "failures": ["x"]}},
           [blocked, _run("order_status_happy", 2, False), _run("order_status_happy", 3, True)])
    data = json.loads((tmp_path / "20261123-100000-all-haiku.json").read_text())
    data["summary"] |= {"git_commit": "abc1234def", "git_dirty": False}
    (tmp_path / "20261123-100000-all-haiku.json").write_text(json.dumps(data))
    [row] = list_runs(tmp_path)
    assert row["git_commit"] == "abc1234def" and row["git_dirty"] is False
    assert row["failure_causes"] == {"supervisor": 1, "assertion": 1}
    assert abs(row["cost_per_conversation_usd"] - 0.01) < 1e-9
    assert [c["cause"] for c in run_detail("20261123-100000-all-haiku", tmp_path)["conversations"]] == \
        ["supervisor", "assertion", None]
    # Clopper-Pearson 95% upper bounds, as cited in docs/EVALS.md.
    assert abs(evals_data.failure_upper95(1, 93) - 0.0585) < 5e-4
    assert abs(evals_data.failure_upper95(0, 24) - 0.1425) < 5e-4


def test_catalog_describes_every_scenario_in_plain_language():
    from bookly.web.evals_data import catalog, describe
    cat = catalog()
    assert cat["sets"] == {"core": 8, "extended": 10, "hard": 13}
    assert all(s["checks"] and s["opening"] for s in cat["scenarios"].values())
    assert describe({"returns": [{"item_id": "I-1", "resolution": "store_credit", "amount_cents": 1899}],
                     "tickets": {"count": 0}, "reply_is_question": [1]}) == [
        "Creates exactly: store credit of $18.99 for I-1", "Ticket: no escalation ticket", "Asks a question in turn 1"]
    assert not any(c.startswith(k) for s in cat["scenarios"].values() for c in s["checks"]
                   for k in ("tool_", "reply_", "event"))  # every assertion key has a description
