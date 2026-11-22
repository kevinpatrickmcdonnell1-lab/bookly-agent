"""Tool registry. Ten narrow tools, each with one job.

TOOL_SCHEMAS is a fixed list sent on every executor request, in the same order, so
the prompt-cache prefix stays stable and thinking blocks stay valid. Scoping per
route happens in the gate, not by changing this list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from bookly.agent import gate
from bookly.agent.state import Session
from bookly.policy.engine import REASONS, RESOLUTIONS
from bookly.tools import account, escalation, knowledge, orders, recs, returns
from bookly.tools.common import ToolDeps, ToolError

Handler = Callable[[Session, dict, ToolDeps], dict]

HANDLERS: dict[str, Handler] = {
    "verify_customer": account.verify_customer,
    "lookup_orders": orders.lookup_orders,
    "get_order_status": orders.get_order_status,
    "search_policy": knowledge.search_policy,
    "check_return_eligibility": returns.check_return_eligibility,
    "propose_return": returns.propose_return,
    "initiate_return": returns.initiate_return,
    "send_password_reset": account.send_password_reset,
    "get_recommendation": recs.get_recommendation,
    "escalate_to_human": escalation.escalate_to_human,
}

WRITE_TOOLS = frozenset({"initiate_return", "send_password_reset", "escalate_to_human"})


def _schema(props: dict[str, Any]) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}

TOOL_SCHEMAS: list[dict] = [
    {"name": "verify_customer",
     "description": "Confirm who the customer is from an order number and the email on that order. Use before "
                    "any account data. Max 3 attempts per conversation.",
     "input_schema": _schema({"order_number": _STR, "email": _STR})},
    {"name": "lookup_orders",
     "description": "List the verified customer's recent orders with item_ids, staff-pick flags and delivery "
                    "dates. Use to find which item the customer means, and to spot when more than one order "
                    "could match.",
     "input_schema": _schema({})},
    {"name": "get_order_status",
     "description": "Get status, carrier, tracking number and delivery estimate for one of the verified "
                    "customer's orders. Use when they ask where an order is.",
     "input_schema": _schema({"order_number": _STR})},
    {"name": "search_policy",
     "description": "Search Bookly's policy docs (returns, ebooks, staff-pick guarantee, shipping, store credit, "
                    "account, hours). Use for any general policy question. Answer only from the results and "
                    "cite the policy_ref.",
     "input_schema": _schema({"query": _STR})},
    {"name": "check_return_eligibility",
     "description": "Ask the policy engine whether an item can be returned and for what. Use once you know the "
                    "item and the customer's reason. Returns eligibility, resolution, amount and policy_ref; "
                    "never decide these yourself.",
     "input_schema": _schema({
         "item_id": _STR,
         "reason": {"type": "string", "enum": list(REASONS)},
         "is_unread": {"type": "boolean", "description": "What the customer said: true if they haven't read it. "
                       "Use false if they didn't say, or if it doesn't matter (damaged, wrong item, downloaded ebook)."},
     })},
    {"name": "propose_return",
     "description": "Record the exact return you are about to offer, after an eligible check. Then tell the "
                    "customer exactly what will happen and ask them to confirm. Does not change anything yet.",
     "input_schema": _schema({"item_id": _STR, "resolution": {"type": "string", "enum": list(RESOLUTIONS)}})},
    {"name": "initiate_return",
     "description": "Carry out a return the customer has clearly confirmed in their latest message, matching "
                    "an earlier propose_return. The amount comes from the policy engine.",
     "input_schema": _schema({"item_id": _STR, "resolution": {"type": "string", "enum": list(RESOLUTIONS)}})},
    {"name": "send_password_reset",
     "description": "Send a password reset link to the email already on the verified account. Use when a "
                    "verified customer can't log in. It can't send to any other address.",
     "input_schema": _schema({})},
    {"name": "get_recommendation",
     "description": "Get one staff pick to suggest, right after a completed return. Use at most once, and never "
                    "when the customer is frustrated.",
     "input_schema": _schema({})},
    {"name": "escalate_to_human",
     "description": "Hand the conversation to a human with a summary. Use when the customer asks for a person, "
                    "a return needs approval, the customer pushes back after you've explained a policy once, a "
                    "parcel is lost, or you can't help.",
     "input_schema": _schema({
         "queue": {"type": "string", "enum": list(escalation.QUEUES)},
         "reason": {"type": "string", "enum": list(escalation.TRIGGERS)},
         "summary": {"type": "string", "description": "2-3 sentences a bookseller can act on without rereading."},
         "suggested_action": _STR,
     })},
]
for _t in TOOL_SCHEMAS:
    _t["strict"] = True

assert [t["name"] for t in TOOL_SCHEMAS] == list(HANDLERS), "schemas and handlers out of sync"


@dataclass
class ToolOutcome:
    kind: str          # ok | denied | tool_error | system_error
    result: dict
    verdict: gate.Verdict

    @property
    def is_error(self) -> bool:
        return self.kind != "ok"


def run_tool(s: Session, name: str, args: dict, deps: ToolDeps) -> ToolOutcome:
    """Gate, then run. Never raises: every failure comes back as a result the model can recover from."""
    if name not in HANDLERS:
        v = gate.deny("unknown_tool", f"Unknown tool {name}.", "Use one of the listed tools.")
        s.ctx.gate_denials += 1
        return ToolOutcome("denied", v.to_error(), v)
    verdict = gate.check(s, name, args, deps.repo, deps.settings.max_verify_attempts)
    if not verdict.allowed:
        s.ctx.gate_denials += 1
        return ToolOutcome("denied", verdict.to_error(), verdict)
    s.note_tool(name)
    try:
        result = HANDLERS[name](s, args, deps)
    except ToolError as exc:
        s.consecutive_tool_errors = 0
        return ToolOutcome("tool_error", exc.to_dict(), verdict)
    except Exception as exc:  # noqa: BLE001 - a broken tool must never crash the conversation
        s.consecutive_tool_errors += 1
        return ToolOutcome("system_error", {
            "error": "system_error", "code": type(exc).__name__, "message": "The tool failed unexpectedly.",
            "how_to_recover": "Apologise briefly and try once more, or offer a bookseller."}, verdict)
    s.consecutive_tool_errors = 0
    s.grounding.append(json.dumps(result, default=str))
    if isinstance(result, dict) and result.get("policy_ref"):
        s.note_policy_ref(result["policy_ref"])
    return ToolOutcome("ok", result, verdict)
