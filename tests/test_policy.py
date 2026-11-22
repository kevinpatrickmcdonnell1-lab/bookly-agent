from datetime import date, timedelta

import pytest

from bookly.policy.engine import ItemFacts, evaluate_return

TODAY = date(2026, 11, 22)


def item(fmt="paperback", price=1899, staff_pick=False, downloaded=False, days_ago=10):
    delivered = None if days_ago is None else TODAY - timedelta(days=days_ago)
    return ItemFacts(fmt, price, staff_pick, downloaded, delivered)


def test_rule1_damaged_always_refunded_even_out_of_window():
    d = evaluate_return(item(days_ago=200), "damaged", is_unread=False, today=TODAY)
    assert (d.eligible, d.resolution, d.amount_cents, d.policy_ref) == (True, "refund", 1899, "returns#damaged")


def test_rule1_wrong_item_beats_downloaded_ebook():
    d = evaluate_return(item(fmt="ebook", downloaded=True), "wrong_item", is_unread=False, today=TODAY)
    assert d.eligible and d.resolution == "refund"


def test_rule2_downloaded_ebook_not_eligible():
    d = evaluate_return(item(fmt="ebook", downloaded=True, days_ago=2), "changed_mind", is_unread=True, today=TODAY)
    assert not d.eligible and d.policy_ref == "ebooks#downloaded" and d.amount_cents == 0


def test_rule2_downloaded_staff_pick_ebook_still_not_eligible():
    d = evaluate_return(item(fmt="ebook", downloaded=True, staff_pick=True), "didnt_enjoy", False, TODAY)
    assert not d.eligible and d.policy_ref == "ebooks#downloaded"


def test_rule3_staff_pick_read_gets_store_credit():
    d = evaluate_return(item(staff_pick=True), "didnt_enjoy", is_unread=False, today=TODAY)
    assert (d.eligible, d.resolution, d.policy_ref) == (True, "store_credit", "guarantee#staff-picks")
    assert d.amount_cents == 1899


def test_rule3_staff_pick_wins_over_refund_even_if_unread():
    d = evaluate_return(item(staff_pick=True), "didnt_enjoy", is_unread=True, today=TODAY)
    assert d.resolution == "store_credit"


@pytest.mark.parametrize("days,eligible", [(60, True), (61, False)])
def test_rule3_guarantee_boundary(days, eligible):
    d = evaluate_return(item(staff_pick=True, days_ago=days), "didnt_enjoy", is_unread=False, today=TODAY)
    assert d.eligible is eligible
    if eligible:
        assert d.resolution == "store_credit"


def test_rule3_needs_didnt_enjoy_reason():
    d = evaluate_return(item(staff_pick=True), "changed_mind", is_unread=False, today=TODAY)
    assert not d.eligible


@pytest.mark.parametrize("days,eligible", [(0, True), (30, True), (31, False)])
def test_rule4_return_window_boundary(days, eligible):
    d = evaluate_return(item(days_ago=days), "changed_mind", is_unread=True, today=TODAY)
    assert d.eligible is eligible
    assert d.policy_ref == "returns#window"


def test_rule4_read_physical_book_not_eligible():
    d = evaluate_return(item(), "didnt_enjoy", is_unread=False, today=TODAY)
    assert not d.eligible and d.policy_ref == "returns#window"


def test_rule4_undownloaded_ebook_refundable():
    d = evaluate_return(item(fmt="ebook", downloaded=False), "changed_mind", is_unread=False, today=TODAY)
    assert d.eligible and d.resolution == "refund"


def test_rule5_hardcover_45_days():
    d = evaluate_return(item(fmt="hardcover", price=3200, days_ago=45), "changed_mind", True, TODAY)
    assert not d.eligible and "30-day" in d.reason


def test_not_delivered_not_eligible():
    d = evaluate_return(item(days_ago=None), "changed_mind", True, TODAY)
    assert not d.eligible


def test_limit_over_cap_requires_approval():
    d = evaluate_return(item(fmt="box_set", price=9000, days_ago=7), "changed_mind", True, TODAY)
    assert d.eligible and d.requires_approval and d.resolution == "refund"
    assert d.policy_ref == "returns#approval" and d.amount_cents == 9000


def test_limit_exactly_at_cap_does_not_require_approval():
    d = evaluate_return(item(price=7500), "changed_mind", True, TODAY)
    assert d.eligible and not d.requires_approval


def test_limit_fourth_return_in_90_days():
    d = evaluate_return(item(), "changed_mind", True, TODAY, prior_returns_90d=3)
    assert d.requires_approval
    d = evaluate_return(item(), "changed_mind", True, TODAY, prior_returns_90d=2)
    assert not d.requires_approval


def test_limit_not_applied_to_ineligible():
    d = evaluate_return(item(price=9000, days_ago=45), "changed_mind", True, TODAY)
    assert not d.eligible and not d.requires_approval


def test_unknown_reason_rejected():
    with pytest.raises(ValueError):
        evaluate_return(item(), "free_money", True, TODAY)
