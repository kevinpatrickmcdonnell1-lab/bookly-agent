"""Session state. The model never holds state that matters: code holds it here and
shows it to the model each turn as a <session_state> block."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


MAX_MENTIONED = 5


@dataclass
class PendingAction:
    item_id: str
    resolution: str
    amount_cents: int
    proposed_turn: int


@dataclass
class TurnContext:
    """Per-turn facts the gate needs. Reset at the start of every turn."""
    route: str = "general"
    write_tools: frozenset[str] = frozenset()
    confirmation: str = "none"          # router's reading of this message: yes | no | none
    frustration: str = "low"
    gate_denials: int = 0
    tool_rounds: int = 0
    tools_called: list[str] = field(default_factory=list)   # passed the gate
    tools_ok: list[str] = field(default_factory=list)       # also returned a result without error


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: list[dict[str, Any]] = field(default_factory=list)
    turn: int = 0

    verified_customer_id: str | None = None
    verify_attempts: int = 0

    active_aop: str | None = None
    queued_intents: list[str] = field(default_factory=list)
    pending_action: PendingAction | None = None
    eligibility: dict[str, dict] = field(default_factory=dict)   # item_id -> engine decision + inputs
    returns_done: list[dict] = field(default_factory=list)
    # What the conversation is about. Code sets these from tool results so that
    # "it", "that one" or "the one you recommended" resolve without asking again.
    focus: dict | None = None                                     # the order/item discussed most recently
    mentioned: list[dict] = field(default_factory=list)           # recent items/orders, newest last
    recommended: dict | None = None                               # the staff pick offered, if any
    password_reset_sent: bool = False
    ineligible_explained: int = 0

    unclear_streak: int = 0
    high_frustration_count: int = 0
    abuse_count: int = 0
    consecutive_tool_errors: int = 0
    escalated_ticket_id: int | None = None

    tools_tried: list[str] = field(default_factory=list)          # across the session, for handoff tickets
    policy_refs: list[str] = field(default_factory=list)
    # Every fact a tool returned this session, as text. The output guard checks that
    # dollar amounts, dates and tracking numbers in a reply appear here.
    grounding: list[str] = field(default_factory=list)
    policy_texts: dict[str, str] = field(default_factory=dict)    # policy_ref -> text seen this session
    # The last reply that went out, after every guard. Voice speaks only this, never caller-supplied text.
    last_reply: str = ""

    ctx: TurnContext = field(default_factory=TurnContext)

    def note_tool(self, name: str) -> None:
        self.ctx.tools_called.append(name)
        if name not in self.tools_tried:
            self.tools_tried.append(name)

    @property
    def recommendation_given(self) -> bool:
        return self.recommended is not None

    def set_focus(self, *, title: str | None = None, order_number: str | None = None,
                  item_id: str | None = None, status: str | None = None, source: str,
                  focus: bool = True) -> None:
        """Record an item or order that was discussed. `focus=False` only adds it to `mentioned`."""
        entry = {k: v for k, v in {"title": title, "order_number": order_number, "item_id": item_id,
                                   "status": status, "source": source}.items() if v is not None}
        key = (entry.get("item_id"), entry.get("order_number"), entry.get("title"))
        self.mentioned = [m for m in self.mentioned
                          if (m.get("item_id"), m.get("order_number"), m.get("title")) != key]
        self.mentioned = (self.mentioned + [entry])[-MAX_MENTIONED:]
        if focus:
            self.focus = entry

    def note_policy_ref(self, ref: str | None) -> None:
        if ref and ref not in self.policy_refs:
            self.policy_refs.append(ref)

    def snapshot(self) -> dict[str, Any]:
        """What the model sees each turn. No raw emails, nothing it could misuse."""
        return {
            "verified": self.verified_customer_id is not None,
            "verified_customer_id": self.verified_customer_id,
            "verify_attempts_left": max(0, 3 - self.verify_attempts),
            "active_aop": self.active_aop,
            "queued_intents": self.queued_intents,
            "eligibility_checked": {
                k: {kk: v.get(kk) for kk in ("eligible", "resolution", "amount_display", "requires_approval",
                                               "policy_ref")}
                for k, v in self.eligibility.items()
            },
            "pending_action": None if not self.pending_action else {
                "item_id": self.pending_action.item_id,
                "resolution": self.pending_action.resolution,
                "amount_display": f"${self.pending_action.amount_cents / 100:.2f}",
                "proposed_on_turn": self.pending_action.proposed_turn,
            },
            "returns_completed_this_session": [r["item_id"] for r in self.returns_done],
            "in_focus": self.focus,
            "recently_discussed": [m for m in self.mentioned if m != self.focus],
            "recommended": self.recommended,
            "recommendation_given": self.recommendation_given,
            "password_reset_sent": self.password_reset_sent,
            "times_ineligible_decision_explained": self.ineligible_explained,
            "escalated_ticket_id": self.escalated_ticket_id,
        }
