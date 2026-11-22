from __future__ import annotations

from bookly.agent.state import Session
from bookly.tools.common import ToolDeps, money, nice_date, untrusted

STATUS_TEXT = {
    "processing": "being packed",
    "shipped": "shipped",
    "in_transit": "on its way",
    "delivered": "delivered",
}


def _items(deps: ToolDeps, order_number: str) -> list[dict]:
    out = []
    for it in deps.repo.items_for_order(order_number):
        out.append({
            "item_id": it["item_id"], "title": it["title"], "author": it["author"], "format": it["format"],
            "price": money(it["price_cents"]), "staff_pick": bool(it["staff_pick"]),
            "downloaded": bool(it["downloaded"]) if it["format"] == "ebook" else None,
            "already_returned": deps.repo.return_for_item(it["item_id"]) is not None,
        })
    return out


RECENT_ORDERS_ON_VERIFY = 5


def recent_orders_summary(deps: ToolDeps, customer_id: str, limit: int = RECENT_ORDERS_ON_VERIFY) -> dict:
    """Compact order list returned with a successful verification, so the model
    doesn't need a separate lookup_orders round. No gift notes or tracking here:
    lookup_orders and get_order_status hold the detail."""
    orders = deps.repo.orders_for_customer(customer_id)
    out = []
    for o in orders[:limit]:
        out.append({
            "order_number": o["order_number"],
            "status": STATUS_TEXT[o["status"]],
            "placed_on": nice_date(o["placed_at"]),
            "delivered_on": nice_date(o["delivered_at"]),
            "items": [{"item_id": i["item_id"], "title": i["title"], "format": i["format"],
                       "staff_pick": i["staff_pick"], "already_returned": i["already_returned"]}
                      for i in _items(deps, o["order_number"])],
        })
    return {"recent_orders": out, "more_orders": max(0, len(orders) - limit)}


def lookup_orders(s: Session, args: dict, deps: ToolDeps) -> dict:
    orders = []
    for o in deps.repo.orders_for_customer(s.verified_customer_id or ""):
        orders.append({
            "order_number": o["order_number"],
            "placed_on": nice_date(o["placed_at"]),
            "status": STATUS_TEXT[o["status"]],
            "delivered_on": nice_date(o["delivered_at"]),
            "items": _items(deps, o["order_number"]),
            "gift_note": untrusted(o["gift_note"]),
        })
    if len(orders) == 1:
        o = orders[0]
        _focus_order(s, o["order_number"], o["status"], o["items"])
    return {"orders": orders, "count": len(orders)}


def _focus_order(s: Session, order_number: str, status: str, items: list[dict]) -> None:
    """Focus on the order; on its item too when there's only one, so "it" is unambiguous."""
    only = items[0] if len(items) == 1 else None
    s.set_focus(order_number=order_number, status=status, source="order_status",
                title=only["title"] if only else None, item_id=only["item_id"] if only else None)


def get_order_status(s: Session, args: dict, deps: ToolDeps) -> dict:
    o = deps.repo.order(str(args["order_number"]))
    assert o is not None  # the gate already checked ownership
    items = _items(deps, o["order_number"])
    result = {
        "order_number": o["order_number"],
        "status": STATUS_TEXT[o["status"]],
        "placed_on": nice_date(o["placed_at"]),
        "items": [i["title"] for i in items],
    }
    _focus_order(s, o["order_number"], STATUS_TEXT[o["status"]], items)
    if o["status"] == "delivered":
        result["delivered_on"] = nice_date(o["delivered_at"])
    else:
        result["estimated_delivery"] = nice_date(o["eta"])
    if o["tracking_number"]:
        result["carrier"] = o["carrier"]
        result["tracking_number"] = o["tracking_number"]
        result["last_scan"] = ("Delivered" if o["status"] == "delivered"
                               else "Departed regional facility")  # mocked carrier data
    return result
