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
    return {"orders": orders, "count": len(orders)}


def get_order_status(s: Session, args: dict, deps: ToolDeps) -> dict:
    o = deps.repo.order(str(args["order_number"]))
    assert o is not None  # the gate already checked ownership
    result = {
        "order_number": o["order_number"],
        "status": STATUS_TEXT[o["status"]],
        "placed_on": nice_date(o["placed_at"]),
        "items": [i["title"] for i in _items(deps, o["order_number"])],
    }
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
