from __future__ import annotations

from bookly.agent.state import Session
from bookly.tools.common import ToolDeps, ToolError, mask_email
from bookly.tools.orders import recent_orders_summary

FAILED = ToolError("verification_failed",
                   "We couldn't match that order number and email.",
                   "Ask the customer to double-check both. Never say whether the order number exists.")


def verify_customer(s: Session, args: dict, deps: ToolDeps) -> dict:
    order_number = str(args.get("order_number", "")).strip()
    email = str(args.get("email", "")).strip()
    customer = deps.repo.find_customer_by_order_and_email(order_number, email) if order_number and email else None
    if customer is None:
        s.verify_attempts += 1
        raise FAILED
    if s.verified_customer_id and s.verified_customer_id != customer["id"]:
        raise ToolError("already_verified",
                        "This conversation is already verified for a different customer.",
                        "Explain that you can help with one account per conversation.")
    s.verified_customer_id = customer["id"]
    return {"verified": True, "customer_id": customer["id"], "first_name": customer["name"].split()[0],
            "email_on_file": mask_email(customer["email"]),
            **recent_orders_summary(deps, customer["id"]),
            "note": "recent_orders is a summary. Call lookup_orders only for older orders or gift notes."}


def send_password_reset(s: Session, args: dict, deps: ToolDeps) -> dict:
    customer = deps.repo.customer(s.verified_customer_id or "")
    if customer is None:
        raise ToolError("not_verified", "No verified customer.", "Verify the customer first.")
    # Always the email on file. The tool has no address parameter by design.
    deps.repo.insert_password_reset(customer["id"], customer["email"])
    s.password_reset_sent = True
    return {"sent": True, "sent_to": mask_email(customer["email"]), "link_expires_in": "30 minutes",
            "policy_ref": "account#password"}
