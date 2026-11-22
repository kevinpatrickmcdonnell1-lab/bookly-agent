import sqlite3

import pytest

from bookly.tools import TOOL_SCHEMAS
from conftest import unlock, verify


def test_schemas_are_strict_and_closed():
    for t in TOOL_SCHEMAS:
        assert t["strict"] is True
        schema = t["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_no_tool_takes_a_money_amount():
    for t in TOOL_SCHEMAS:
        for prop in t["input_schema"]["properties"]:
            assert "amount" not in prop and "cents" not in prop and "price" not in prop


def test_password_reset_has_no_address_parameter():
    [t] = [t for t in TOOL_SCHEMAS if t["name"] == "send_password_reset"]
    assert t["input_schema"]["properties"] == {}


def test_lookup_orders_wraps_gift_note_as_data(call, session):
    verify(call, session, "4812", "ben.okafor@example.com")
    out = call(session, "lookup_orders")
    notes = [o["gift_note"] for o in out.result["orders"] if o["gift_note"]]
    assert notes and notes[0].startswith("<customer_written_data")


def test_ana_has_two_recent_orders(call, session):
    verify(call, session)
    orders = call(session, "lookup_orders").result["orders"]
    assert {o["order_number"] for o in orders} == {"4801", "4802"}
    staff = [i for o in orders for i in o["items"] if i["staff_pick"]]
    assert [i["title"] for i in staff] == ["The Overstory"]


def test_order_status_in_transit_has_tracking(call, session, repo):
    verify(call, session, "4821", "chloe.park@example.com")
    r = call(session, "get_order_status", order_number="4821").result
    assert r["tracking_number"] == repo.order("4821")["tracking_number"]
    assert "estimated_delivery" in r
    assert r["tracking_number"] in " ".join(session.grounding)


def test_damaged_book_full_refund(call, session):
    verify(call, session, "4821", "chloe.park@example.com")
    r = call(session, "check_return_eligibility", item_id="I-4822-1", reason="damaged", is_unread=False).result
    assert (r["eligible"], r["resolution"], r["amount_display"]) == (True, "refund", "$20.00")


def test_ben_hardcover_out_of_window(call, session):
    verify(call, session, "4813", "ben.okafor@example.com")
    r = call(session, "check_return_eligibility", item_id="I-4813-1", reason="changed_mind", is_unread=True).result
    assert not r["eligible"] and r["policy_ref"] == "returns#window"


def test_second_check_after_return_reports_already_returned(call, session):
    verify(call, session, "4821", "chloe.park@example.com")
    unlock(session, "returns")
    call(session, "check_return_eligibility", item_id="I-4822-1", reason="damaged", is_unread=False)
    call(session, "propose_return", item_id="I-4822-1", resolution="refund")
    session.turn += 1
    session.ctx.confirmation = "yes"
    assert call(session, "initiate_return", item_id="I-4822-1", resolution="refund").kind == "ok"
    again = call(session, "check_return_eligibility", item_id="I-4822-1", reason="damaged", is_unread=False)
    assert again.result["eligible"] is False and again.result["policy_ref"] == "returns#one-per-item"


def test_escalation_ticket_has_context(call, session, repo):
    verify(call, session, "4812", "ben.okafor@example.com")
    call(session, "check_return_eligibility", item_id="I-4812-1", reason="changed_mind", is_unread=False)
    out = call(session, "escalate_to_human", queue="booksellers", reason="policy_pushback",
               summary="Wants a refund on a downloaded ebook.", suggested_action="Review; goodwill credit at discretion.")
    [t] = repo.tickets()
    assert t["customer"] == "verified: C-1042 (Ben Okafor)"
    assert "ebooks#downloaded" in t["policy_refs"]
    assert "check_return_eligibility" in t["tried"]
    assert "email reply" in out.result["customer_message"]


def test_tool_crash_becomes_system_error(call, session, monkeypatch):
    from bookly import tools

    def boom(*a, **k):
        raise RuntimeError("carrier API timeout")
    monkeypatch.setitem(tools.HANDLERS, "search_policy", boom)
    out = call(session, "search_policy", query="x")
    assert out.kind == "system_error" and session.consecutive_tool_errors == 1


# --- database backstops --------------------------------------------------------

def test_db_one_return_per_item(repo):
    kw = dict(item_id="I-4801-1", customer_id="C-1001", resolution="store_credit", amount_cents=1899,
              reason="didnt_enjoy", policy_ref="guarantee#staff-picks")
    repo.insert_return(**kw, idempotency_key="a")
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_return(**kw, idempotency_key="b")


def test_db_blocks_refund_on_downloaded_ebook(repo):
    with pytest.raises(sqlite3.IntegrityError, match="downloaded ebooks"):
        repo.insert_return(item_id="I-4812-1", customer_id="C-1042", resolution="refund", amount_cents=1499,
                           reason="changed_mind", policy_ref="returns#window", idempotency_key="k")


def test_db_rejects_bad_resolution_and_zero_amount(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_return(item_id="I-4802-1", customer_id="C-1001", resolution="cash", amount_cents=100,
                           reason="other", policy_ref="x", idempotency_key="k1")
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_return(item_id="I-4802-1", customer_id="C-1001", resolution="refund", amount_cents=0,
                           reason="other", policy_ref="x", idempotency_key="k2")


# --- policy search -------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("Do you ship to Canada?", "shipping#international"),
    ("can I return an ebook I downloaded", "ebooks#downloaded"),
    ("staff pick didn't love it", "guarantee#staff-picks"),
    ("forgot my password can't log in", "account#password"),
])
def test_search_policy_finds_right_section(deps, query, expected):
    assert deps.kb.search(query)[0].id == expected


def test_every_engine_policy_ref_exists_in_kb(deps):
    for ref in ("returns#window", "returns#damaged", "returns#approval", "returns#one-per-item",
                "ebooks#downloaded", "guarantee#staff-picks", "account#password"):
        assert ref in deps.kb.by_id, ref


def test_verify_returns_recent_orders_summary_without_gift_notes(call, session):
    out = call(session, "verify_customer", order_number="4813", email="ben.okafor@example.com").result
    assert {o["order_number"] for o in out["recent_orders"]} == {"4812", "4813"}
    assert out["recent_orders"][0]["items"][0]["item_id"].startswith("I-48")
    assert "gift_note" not in str(out) and "tracking" not in str(out)
    assert out["more_orders"] == 0
