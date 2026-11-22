"""Tool gate. Every tool call the model makes is checked here, in code, before it runs.

The model cannot override a denial. A denial comes back to the model as a tool
error that says how to recover ("ask the customer to confirm first"), so the model
can repair the conversation rather than loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from bookly.agent.state import Session
from bookly.data.repo import Repo

# Tools that change something, or set up a change, are unlocked per route.
ROUTE_WRITE_TOOLS: dict[str, frozenset[str]] = {
    "returns": frozenset({"propose_return", "initiate_return"}),
    "account_access": frozenset({"send_password_reset"}),
}
SCOPED_TOOLS = frozenset().union(*ROUTE_WRITE_TOOLS.values())
NEVER_BLOCKED = frozenset({"escalate_to_human"})


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    code: str = "ok"
    message: str = ""
    how_to_recover: str = ""

    def to_error(self) -> dict:
        return {"error": "gate_denied", "code": self.code, "message": self.message,
                "how_to_recover": self.how_to_recover}


OK = Verdict(True)


def deny(code: str, message: str, how_to_recover: str) -> Verdict:
    return Verdict(False, code, message, how_to_recover)


def write_tools_for(intents: list[str] | tuple[str, ...]) -> frozenset[str]:
    aop_for = {"return_refund": "returns", "account_access": "account_access"}
    tools: set[str] = set()
    for intent in intents:
        tools |= ROUTE_WRITE_TOOLS.get(aop_for.get(intent, ""), frozenset())
    return frozenset(tools)


def _require_verified(s: Session) -> Verdict | None:
    if s.verified_customer_id is None:
        return deny("not_verified", "The customer hasn't been verified yet.",
                    "Ask for their order number and the email on the order, then call verify_customer.")
    return None


def _require_item_owned(s: Session, repo: Repo, item_id: str) -> Verdict | None:
    it = repo.item(item_id or "")
    if it is None or it["customer_id"] != s.verified_customer_id:
        return deny("item_not_found", "That item isn't on this customer's account.",
                    "Call lookup_orders to get the correct item_id. Don't reveal anything about other accounts.")
    return None


def check(s: Session, tool: str, args: dict, repo: Repo, max_verify_attempts: int = 3) -> Verdict:
    if tool in NEVER_BLOCKED:
        return OK

    if tool in SCOPED_TOOLS and tool not in s.ctx.write_tools:
        return deny("out_of_scope", f"{tool} isn't available for this request (route: {s.ctx.route}).",
                    "Only use this tool when the customer is asking for it. Answer the question they asked.")

    if tool == "verify_customer":
        if s.verify_attempts >= max_verify_attempts:
            return deny("too_many_attempts", "Verification has failed too many times in this session.",
                        "Don't try again. Offer to connect the customer with a bookseller (escalate_to_human).")
        return OK

    if tool == "search_policy":
        return OK

    if (v := _require_verified(s)) is not None:
        return v

    if tool == "lookup_orders":
        return OK

    if tool == "get_order_status":
        order = repo.order(str(args.get("order_number", "")))
        if order is None or order["customer_id"] != s.verified_customer_id:
            return deny("order_not_found", "That order isn't on this customer's account.",
                        "Tell the customer you can only discuss orders on the verified account. "
                        "Don't confirm whether the order exists.")
        return OK

    if tool == "check_return_eligibility":
        return _require_item_owned(s, repo, args.get("item_id", "")) or OK

    if tool == "propose_return":
        item_id = args.get("item_id", "")
        if (v := _require_item_owned(s, repo, item_id)) is not None:
            return v
        decision = s.eligibility.get(item_id)
        if decision is None:
            return deny("eligibility_not_checked", "Eligibility hasn't been checked for this item.",
                        "Call check_return_eligibility first.")
        if not decision["eligible"]:
            return deny("not_eligible", "This item isn't eligible for a return.",
                        "Explain the policy once, kindly, and offer a bookseller.")
        if decision["requires_approval"]:
            return deny("requires_approval", "This return needs a bookseller's approval.",
                        "Call escalate_to_human with queue order_ops and the recommended resolution.")
        if args.get("resolution") != decision["resolution"]:
            return deny("resolution_mismatch",
                        f"The policy allows {decision['resolution']} for this item, not {args.get('resolution')}.",
                        f"Propose {decision['resolution']} instead, or offer a bookseller if the customer disagrees.")
        return OK

    if tool == "initiate_return":
        item_id = args.get("item_id", "")
        if (v := _require_item_owned(s, repo, item_id)) is not None:
            return v
        p = s.pending_action
        if p is None or p.item_id != item_id or p.resolution != args.get("resolution"):
            return deny("no_matching_proposal", "There's no matching proposal for this return.",
                        "Call propose_return, tell the customer exactly what will happen, and ask them to confirm.")
        if p.proposed_turn >= s.turn:
            return deny("confirm_in_next_turn", "The proposal was made in this turn; the customer hasn't confirmed.",
                        "Ask the customer to confirm, then wait for their reply.")
        if s.ctx.confirmation != "yes":
            return deny("not_confirmed", "The customer's latest message isn't a clear yes.",
                        "Ask the customer whether they'd like to go ahead.")
        decision = s.eligibility.get(item_id) or {}
        if decision.get("requires_approval"):
            return deny("requires_approval", "This return needs a bookseller's approval.",
                        "Call escalate_to_human with queue order_ops.")
        return OK

    if tool == "send_password_reset":
        return OK

    if tool == "get_recommendation":
        if not s.returns_done:
            return deny("no_return_completed", "Recommendations follow a completed return.",
                        "Only recommend after a return has been completed.")
        if s.recommendation_given:
            return deny("already_recommended", "A recommendation was already given in this session.",
                        "Don't recommend again.")
        if s.ctx.frustration == "high":
            return deny("customer_frustrated", "The customer is frustrated; this isn't the moment to sell.",
                        "Skip the recommendation.")
        return OK

    return deny("unknown_tool", f"Unknown tool {tool}.", "Use one of the listed tools.")
