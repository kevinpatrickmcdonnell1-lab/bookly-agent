"""Every gate precondition has a passing and a failing test."""

from bookly.agent.state import TurnContext
from conftest import unlock, verify


def next_turn(s, confirmation="none", route="returns", frustration="low"):
    s.turn += 1
    s.ctx = TurnContext()
    s.ctx.confirmation = confirmation
    s.ctx.frustration = frustration
    unlock(s, route)


def eligible_staff_pick(call, s):
    verify(call, s)
    unlock(s, "returns")
    out = call(s, "check_return_eligibility", item_id="I-4801-1", reason="didnt_enjoy", is_unread=False)
    assert out.kind == "ok" and out.result["resolution"] == "store_credit"


# --- verify_customer ---------------------------------------------------------

def test_verify_ok(call, session):
    assert verify(call, session).result["email_on_file"] == "a•••@example.com"


def test_verify_same_message_for_unknown_order_and_wrong_email(call, session):
    wrong_email = call(session, "verify_customer", order_number="4801", email="x@example.com")
    no_order = call(session, "verify_customer", order_number="9999", email="ana.ruiz@example.com")
    assert wrong_email.result == no_order.result
    assert wrong_email.result["code"] == "verification_failed"


def test_verify_locked_after_three_failures(call, session):
    for _ in range(3):
        call(session, "verify_customer", order_number="4801", email="nope@example.com")
    out = call(session, "verify_customer", order_number="4801", email="ana.ruiz@example.com")
    assert out.kind == "denied" and out.verdict.code == "too_many_attempts"
    assert session.verified_customer_id is None


# --- reads need verification ---------------------------------------------------

def test_lookup_orders_requires_verification(call, session):
    assert call(session, "lookup_orders").verdict.code == "not_verified"
    verify(call, session)
    assert call(session, "lookup_orders").kind == "ok"


def test_search_policy_never_needs_verification(call, session):
    assert call(session, "search_policy", query="ship to Canada").kind == "ok"


def test_order_status_only_for_own_orders(call, session):
    verify(call, session)
    assert call(session, "get_order_status", order_number="4801").kind == "ok"
    other = call(session, "get_order_status", order_number="4812")   # Ben's order
    assert other.kind == "denied" and other.verdict.code == "order_not_found"
    assert "Ben" not in str(other.result) and "Hail Mary" not in str(other.result)


def test_eligibility_only_for_own_items(call, session):
    verify(call, session)
    assert call(session, "check_return_eligibility", item_id="I-4812-1", reason="damaged",
                is_unread=True).verdict.code == "item_not_found"
    assert call(session, "check_return_eligibility", item_id="I-4801-1", reason="didnt_enjoy",
                is_unread=False).kind == "ok"


# --- scope -------------------------------------------------------------------

def test_write_tools_locked_outside_their_route(call, session):
    verify(call, session)
    unlock(session, "policy_qa")
    assert call(session, "propose_return", item_id="I-4801-1", resolution="store_credit").verdict.code == "out_of_scope"
    assert call(session, "send_password_reset").verdict.code == "out_of_scope"


def test_escalation_never_blocked(call, session):
    out = call(session, "escalate_to_human", queue="booksellers", reason="customer_request",
               summary="Wants a person.", suggested_action="Reply.")
    assert out.kind == "ok"


# --- propose_return ----------------------------------------------------------

def test_propose_requires_eligibility_check(call, session):
    verify(call, session)
    unlock(session, "returns")
    out = call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    assert out.verdict.code == "eligibility_not_checked"


def test_propose_resolution_must_match_engine(call, session):
    eligible_staff_pick(call, session)
    cash = call(session, "propose_return", item_id="I-4801-1", resolution="refund")
    assert cash.verdict.code == "resolution_mismatch"
    ok = call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    assert ok.kind == "ok" and session.pending_action.amount_cents == 1899


def test_propose_denied_when_not_eligible(call, session):
    verify(call, session, "4812", "ben.okafor@example.com")
    unlock(session, "returns")
    call(session, "check_return_eligibility", item_id="I-4812-1", reason="changed_mind", is_unread=True)
    assert call(session, "propose_return", item_id="I-4812-1", resolution="refund").verdict.code == "not_eligible"


def test_propose_denied_when_approval_needed(call, session):
    verify(call, session, "4831", "dev.shah@example.com")
    unlock(session, "returns")
    r = call(session, "check_return_eligibility", item_id="I-4831-1", reason="changed_mind", is_unread=True)
    assert r.result["requires_approval"] is True
    assert call(session, "propose_return", item_id="I-4831-1", resolution="refund").verdict.code == "requires_approval"


# --- initiate_return ---------------------------------------------------------

def test_initiate_requires_proposal(call, session, repo):
    eligible_staff_pick(call, session)
    next_turn(session, confirmation="yes")
    out = call(session, "initiate_return", item_id="I-4801-1", resolution="store_credit")
    assert out.verdict.code == "no_matching_proposal" and repo.returns() == []


def test_initiate_not_in_same_turn_as_proposal(call, session, repo):
    eligible_staff_pick(call, session)
    session.ctx.confirmation = "yes"
    call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    out = call(session, "initiate_return", item_id="I-4801-1", resolution="store_credit")
    assert out.verdict.code == "confirm_in_next_turn" and repo.returns() == []


def test_initiate_requires_yes(call, session, repo):
    eligible_staff_pick(call, session)
    call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    next_turn(session, confirmation="none")
    assert call(session, "initiate_return", item_id="I-4801-1", resolution="store_credit").verdict.code == "not_confirmed"
    assert repo.returns() == []


def test_initiate_ok_after_confirmed_proposal(call, session, repo):
    eligible_staff_pick(call, session)
    call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    next_turn(session, confirmation="yes")
    out = call(session, "initiate_return", item_id="I-4801-1", resolution="store_credit")
    assert out.kind == "ok", out.result
    [row] = repo.returns()
    assert (row["resolution"], row["amount_cents"], row["policy_ref"]) == ("store_credit", 1899, "guarantee#staff-picks")
    assert session.pending_action is None


def test_initiate_resolution_must_match_proposal(call, session):
    eligible_staff_pick(call, session)
    call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    next_turn(session, confirmation="yes")
    assert call(session, "initiate_return", item_id="I-4801-1", resolution="refund").verdict.code == "no_matching_proposal"


# --- send_password_reset -----------------------------------------------------

def test_password_reset_requires_verification(call, session, repo):
    unlock(session, "account_access")
    assert call(session, "send_password_reset").verdict.code == "not_verified"
    verify(call, session, "4831", "dev.shah@example.com")
    out = call(session, "send_password_reset")
    assert out.kind == "ok" and repo.password_resets()[0]["sent_to"] == "dev.shah@example.com"


# --- get_recommendation ------------------------------------------------------

def test_recommendation_only_after_return_and_once(call, session):
    eligible_staff_pick(call, session)
    assert call(session, "get_recommendation").verdict.code == "no_return_completed"
    call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    next_turn(session, confirmation="yes")
    call(session, "initiate_return", item_id="I-4801-1", resolution="store_credit")
    first = call(session, "get_recommendation")
    assert first.kind == "ok" and first.result["title"] not in ("The Overstory",)
    assert call(session, "get_recommendation").verdict.code == "already_recommended"


def test_no_recommendation_when_frustrated(call, session):
    eligible_staff_pick(call, session)
    call(session, "propose_return", item_id="I-4801-1", resolution="store_credit")
    next_turn(session, confirmation="yes", frustration="high")
    call(session, "initiate_return", item_id="I-4801-1", resolution="store_credit")
    assert call(session, "get_recommendation").verdict.code == "customer_frustrated"


def test_unknown_tool_denied(call, session):
    assert call(session, "refund_everything").kind == "denied"
