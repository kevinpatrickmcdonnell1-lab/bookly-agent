from __future__ import annotations

from bookly.agent.state import Session
from bookly.tools.common import ToolDeps, ToolError, money


def get_recommendation(s: Session, args: dict, deps: ToolDeps) -> dict:
    """One staff pick, chosen from first-party order history: same genre as the returned
    book if possible, never something the customer already owns."""
    returned = deps.repo.item(s.returns_done[-1]["item_id"])
    owned = deps.repo.owned_catalog_ids(s.verified_customer_id or "")
    picks = [p for p in deps.repo.staff_picks() if p["id"] not in owned]
    if not picks:
        raise ToolError("no_recommendation", "No suitable staff pick found.", "Skip the recommendation.")
    same_genre = [p for p in picks if returned and p["genre"] == returned["genre"]]
    pick = (same_genre or picks)[0]
    s.recommended = {"title": pick["title"], "author": pick["author"], "staff_pick": True}
    s.set_focus(title=pick["title"], source="recommendation", focus=False)
    return {"title": pick["title"], "author": pick["author"], "why": pick["blurb"],
            "price": money(pick["price_cents"]), "staff_pick": True,
            "note": "Offer this once, lightly. Don't push."}
