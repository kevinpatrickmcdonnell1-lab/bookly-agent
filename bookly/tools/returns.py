from __future__ import annotations

import sqlite3
from datetime import date

from bookly.agent.state import PendingAction, Session
from bookly.policy.engine import Decision, ItemFacts, evaluate_return
from bookly.tools.common import ToolDeps, ToolError, money


def _decide(s: Session, deps: ToolDeps, item_id: str, reason: str, is_unread: bool) -> tuple[dict, Decision]:
    it = deps.repo.item(item_id)
    assert it is not None  # the gate checked ownership
    facts = ItemFacts(
        format=it["format"], price_cents=it["price_cents"], staff_pick=bool(it["staff_pick"]),
        downloaded=bool(it["downloaded"]),
        delivered_on=date.fromisoformat(it["delivered_at"]) if it["delivered_at"] else None,
    )
    decision = evaluate_return(
        facts, reason, is_unread, today=deps.repo.today(),
        prior_returns_90d=deps.repo.returns_in_last_days(it["customer_id"], 90),
        approval_cap_cents=deps.settings.approval_cap_cents,
        max_returns_90d=deps.settings.max_returns_90d_without_approval,
    )
    return it, decision


def _focus_item(s: Session, it, source: str) -> None:
    s.set_focus(title=it["title"], order_number=it["order_number"], item_id=it["item_id"], source=source)


def check_return_eligibility(s: Session, args: dict, deps: ToolDeps) -> dict:
    item_id, reason, is_unread = args["item_id"], args["reason"], bool(args.get("is_unread"))
    it = deps.repo.item(item_id)
    assert it is not None
    _focus_item(s, it, "eligibility")
    if deps.repo.return_for_item(item_id) is not None:
        result = {"item_id": item_id, "title": it["title"], "eligible": False, "resolution": None,
                  "amount_display": None, "requires_approval": False, "policy_ref": "returns#one-per-item",
                  "explanation": "This item has already been returned."}
        s.eligibility[item_id] = result | {"amount_cents": 0}
        s.note_policy_ref(result["policy_ref"])
        return result

    it, d = _decide(s, deps, item_id, reason, is_unread)
    result = {
        "item_id": item_id, "title": it["title"], "eligible": d.eligible, "resolution": d.resolution,
        "amount_display": money(d.amount_cents) if d.eligible else None,
        "requires_approval": d.requires_approval, "policy_ref": d.policy_ref, "explanation": d.reason,
        "days_since_delivery": d.days_since_delivery,
    }
    if d.requires_approval:
        result["next_step"] = ("Don't propose this return. Call escalate_to_human with queue order_ops and "
                               f"suggested_action 'Approve {d.resolution} of {money(d.amount_cents)}'.")
    elif not d.eligible:
        result["next_step"] = "Explain the policy once, kindly, and offer to connect a bookseller."
    else:
        result["next_step"] = "Call propose_return with this resolution."
    s.eligibility[item_id] = result | {"amount_cents": d.amount_cents, "reason_given": reason,
                                       "is_unread": is_unread}
    s.note_policy_ref(d.policy_ref)
    return result


def _describe(title: str, resolution: str, amount: str, reason: str) -> str:
    if resolution == "store_credit":
        return (f"Return {title} for {amount} in Bookly store credit. The credit is added to the account right "
                "away and never expires.")
    send_back = ("There's no need to send the book back." if reason in ("damaged", "wrong_item")
                 else "We'll email a prepaid return label.")
    return (f"Refund {amount} for {title} to the original payment method, which takes 5 to 7 business days. "
            f"{send_back}")


def propose_return(s: Session, args: dict, deps: ToolDeps) -> dict:
    item_id, resolution = args["item_id"], args["resolution"]
    decision = s.eligibility[item_id]
    s.pending_action = PendingAction(item_id, resolution, decision["amount_cents"], s.turn)
    return {
        "proposal": _describe(decision["title"], resolution, decision["amount_display"],
                              decision.get("reason_given", "other")),
        "amount_display": decision["amount_display"],
        "next_step": "Tell the customer exactly this and ask if they'd like to go ahead. "
                     "Don't call initiate_return until they reply yes.",
    }


def initiate_return(s: Session, args: dict, deps: ToolDeps) -> dict:
    item_id, resolution = args["item_id"], args["resolution"]
    decision = s.eligibility[item_id]
    # Re-run the engine at write time: the amount written is the engine's, never the model's.
    _, fresh = _decide(s, deps, item_id, decision.get("reason_given", "other"), decision.get("is_unread", False))
    if not fresh.eligible or fresh.requires_approval or fresh.resolution != resolution:
        raise ToolError("policy_changed", "The return is no longer eligible as proposed.",
                        "Call check_return_eligibility again and explain the result.")
    try:
        return_id = deps.repo.insert_return(
            item_id=item_id, customer_id=s.verified_customer_id or "", resolution=resolution,
            amount_cents=fresh.amount_cents, reason=decision.get("reason_given", "other"),
            policy_ref=fresh.policy_ref, idempotency_key=f"{s.id}:{item_id}",
        )
    except sqlite3.IntegrityError as exc:
        raise ToolError("database_rejected", f"The return couldn't be recorded ({exc}).",
                        "Tell the customer this item can't be returned again, and offer a bookseller.") from exc
    s.pending_action = None
    it = deps.repo.item(item_id)
    if it is not None:
        _focus_item(s, it, "return")
    s.returns_done.append({"item_id": item_id, "return_id": return_id, "resolution": resolution,
                           "amount_cents": fresh.amount_cents})
    return {"return_id": return_id, "status": "completed", "resolution": resolution,
            "amount_display": money(fresh.amount_cents), "title": decision["title"],
            "what_happens_next": _describe(decision["title"], resolution, money(fresh.amount_cents),
                                           decision.get("reason_given", "other"))}
