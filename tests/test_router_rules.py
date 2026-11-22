import pytest

from bookly.agent.router import RouterOutput, decide, rule_signals
from bookly.agent.state import PendingAction, Session
from bookly.config import Settings

S = Settings()


@pytest.mark.parametrize("text", [
    "Let me talk to a person", "can I speak with a human please", "I want a real person", "agent",
    "Representative!", "get me a human", "transfer me to a live agent", "Can I chat with a bookseller?",
])
def test_human_request_rule_matches(text):
    assert rule_signals(text).human_request


@pytest.mark.parametrize("text", [
    "Your agent promised me a refund", "The last agent said it was fine", "I'm a real person who loves books",
    "Where's my order?", "Is there a human element to your recommendations?",
])
def test_human_request_rule_ignores_mentions(text):
    assert not rule_signals(text).human_request


def test_entity_extraction():
    r = rule_signals("Order #4801, email Ana.Ruiz@example.com, it cost $18.99")
    assert r.order_numbers == ["4801"] and r.emails == ["Ana.Ruiz@example.com"]


def out(intents, conf=0.9, confirmation="none", frustration="low"):
    return RouterOutput(intents=intents, confidence=conf, confirmation=confirmation, frustration=frustration)


def test_rule_escalates_before_model():
    d = decide(out(["order_status"]), Session(), rule_signals("let me talk to a person"), S)
    assert d.escalate == "customer_request"


def test_sensitive_goes_to_trust_and_safety():
    d = decide(out(["sensitive"]), Session(), rule_signals("x"), S)
    assert d.escalate == "sensitive_topic" and d.queue == "trust_safety"


def test_fail_safe_when_router_down():
    d = decide(None, Session(), rule_signals("x"), S)
    assert d.aop == "general" and d.mode == "fail_safe" and d.write_tools == frozenset()


def test_low_confidence_clarifies_then_offers_options():
    s = Session()
    d = decide(out(["order_status"], conf=0.4), s, rule_signals("x"), S)
    assert d.mode == "clarify" and not d.write_tools
    s.unclear_streak = 1
    assert decide(out(["unclear"], conf=0.3), s, rule_signals("x"), S).mode == "offer_options"


def test_returns_unlocks_only_return_writes():
    d = decide(out(["return_refund"]), Session(), rule_signals("x"), S)
    assert d.aop == "returns" and d.write_tools == {"propose_return", "initiate_return"}


def test_policy_question_unlocks_no_writes():
    d = decide(out(["policy_question"]), Session(), rule_signals("x"), S)
    assert d.aop == "policy_qa" and d.write_tools == frozenset()


def test_sticky_aop_holds_on_weak_switch():
    s = Session(active_aop="returns")
    d = decide(out(["order_status"], conf=0.7), s, rule_signals("x"), S)
    assert d.aop == "returns"


def test_strong_switch_moves_and_queues_return():
    s = Session(active_aop="returns")
    s.eligibility["I-4801-1"] = {"eligible": True}
    d = decide(out(["policy_question"], conf=0.95), s, rule_signals("x"), S)
    assert d.aop == "policy_qa" and d.queued_intents == ["return_refund"]


def test_bare_yes_with_pending_proposal_goes_to_returns():
    s = Session(active_aop="policy_qa")
    s.pending_action = PendingAction("I-4801-1", "store_credit", 1899, proposed_turn=1)
    d = decide(out(["unclear"], conf=0.3, confirmation="yes"), s, rule_signals("yes"), S)
    assert d.aop == "returns" and d.confirmation == "yes" and "initiate_return" in d.write_tools


def test_confirmation_ignored_without_pending_proposal():
    d = decide(out(["return_refund"], confirmation="yes"), Session(), rule_signals("yes"), S)
    assert d.confirmation == "none"


def test_multi_intent_queues_second():
    d = decide(out(["order_status", "return_refund"]), Session(), rule_signals("x"), S)
    assert d.aop == "order_status" and d.queued_intents == ["return_refund"]
    assert "initiate_return" in d.write_tools and d.notes


def test_out_of_scope():
    d = decide(out(["out_of_scope"], conf=0.9), Session(), rule_signals("x"), S)
    assert d.aop == "general" and d.mode == "out_of_scope"
