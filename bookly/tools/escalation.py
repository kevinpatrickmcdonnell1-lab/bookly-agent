"""Escalation: a designed outcome, not a failure. Writes a ticket a human bookseller
sees in the Agent Inbox, with enough context that the customer never repeats themselves."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bookly.agent.state import Session
from bookly.config import Settings
from bookly.tools.common import ToolDeps

QUEUES = ("booksellers", "order_ops", "trust_safety")
TRIGGERS = ("customer_request", "sensitive_topic", "policy_pushback", "needs_approval", "lost_or_damaged_in_transit",
            "repeated_frustration", "loop_limit", "system_failure", "bad_actor", "other")
QUEUE_NAMES = {"booksellers": "our bookseller team", "order_ops": "our order team",
               "trust_safety": "our customer care team"}


def reply_time_text(settings: Settings, now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if settings.business_open_hour_et <= now.hour < settings.business_close_hour_et:
        return f"You'll get an email reply within {settings.reply_time_hours} hours."
    return (f"Our booksellers are in from {settings.business_open_hour_et}am to "
            f"{settings.business_close_hour_et - 12}pm Eastern, so you'll get an email reply within "
            f"{settings.reply_time_hours} hours of opening.")


def customer_label(s: Session, deps: ToolDeps) -> str:
    if not s.verified_customer_id:
        return "unverified"
    c = deps.repo.customer(s.verified_customer_id)
    return f"verified: {c['id']} ({c['name']})" if c else "unverified"


def create_ticket(s: Session, deps: ToolDeps, *, queue: str, trigger: str, summary: str,
                  suggested_action: str, intent: str | None = None) -> dict:
    queue = queue if queue in QUEUES else "booksellers"
    trigger = trigger if trigger in TRIGGERS else "other"
    sentiment = {"high": "frustrated", "medium": "annoyed"}.get(s.ctx.frustration, "calm")
    priority = "high" if queue == "trust_safety" or s.ctx.frustration == "high" else "normal"
    ticket_id = deps.repo.insert_ticket(
        session_id=s.id, queue=queue, priority=priority, customer=customer_label(s, deps),
        intent=intent or s.active_aop or "unknown", trigger=trigger, summary=summary,
        tried=list(s.tools_tried), policy_refs=list(s.policy_refs), sentiment=sentiment,
        suggested_action=suggested_action,
    )
    s.escalated_ticket_id = ticket_id
    message = (f"I've passed this to {QUEUE_NAMES[queue]} with everything we've discussed. "
               f"{reply_time_text(deps.settings)}")
    return {"ticket_id": ticket_id, "queue": queue, "priority": priority, "customer_message": message}


def escalate_to_human(s: Session, args: dict, deps: ToolDeps) -> dict:
    result = create_ticket(
        s, deps, queue=args.get("queue", "booksellers"), trigger=args.get("reason", "other"),
        summary=str(args.get("summary", "")).strip() or "Customer needs a bookseller.",
        suggested_action=str(args.get("suggested_action", "")).strip() or "Review and reply to the customer.",
    )
    result["next_step"] = "Tell the customer what happens next, using customer_message. Don't promise outcomes."
    return result
