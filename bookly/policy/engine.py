"""Return policy engine. Pure functions: no I/O, no LLM, no clock.

The model never decides eligibility, resolution or amount. It only reports what the
customer said (reason, whether the book is unread); this module turns that plus the
order facts into a decision. Each decision carries a `policy_ref` that matches a
section id in data/kb/, so the citation the agent gives comes from the same rule
that made the decision.

Rules, checked in order:
  0. Not delivered yet                                -> not eligible   returns#window
  1. Reason is damaged or wrong item                  -> refund          returns#damaged
  2. Ebook that has been downloaded                   -> not eligible   ebooks#downloaded
  3. Staff pick, didn't enjoy it, within 60 days      -> store credit   guarantee#staff-picks
  4. Unread physical book (or ebook not downloaded),
     within 30 days of delivery                       -> refund          returns#window
  5. Anything else                                    -> not eligible   returns#window
  Limit: eligible and (amount > cap, or this would be the 4th return in 90 days)
         -> requires_approval                                            returns#approval
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

Reason = Literal["damaged", "wrong_item", "didnt_enjoy", "changed_mind", "other"]
Resolution = Literal["refund", "store_credit"]

REASONS: tuple[str, ...] = ("damaged", "wrong_item", "didnt_enjoy", "changed_mind", "other")
RESOLUTIONS: tuple[str, ...] = ("refund", "store_credit")

RETURN_WINDOW_DAYS = 30
GUARANTEE_WINDOW_DAYS = 60
DEFAULT_APPROVAL_CAP_CENTS = 7500
DEFAULT_MAX_RETURNS_90D = 3


@dataclass(frozen=True)
class ItemFacts:
    format: str                 # paperback | hardcover | ebook | box_set
    price_cents: int
    staff_pick: bool
    downloaded: bool
    delivered_on: date | None


@dataclass(frozen=True)
class Decision:
    eligible: bool
    resolution: Resolution | None
    amount_cents: int
    requires_approval: bool
    policy_ref: str
    reason: str
    days_since_delivery: int | None

    def to_dict(self) -> dict:
        return asdict(self)


def _no(ref: str, why: str, days: int | None) -> Decision:
    return Decision(False, None, 0, False, ref, why, days)


def evaluate_return(
    item: ItemFacts,
    reason: str,
    is_unread: bool,
    today: date,
    prior_returns_90d: int = 0,
    approval_cap_cents: int = DEFAULT_APPROVAL_CAP_CENTS,
    max_returns_90d: int = DEFAULT_MAX_RETURNS_90D,
) -> Decision:
    if reason not in REASONS:
        raise ValueError(f"unknown reason: {reason!r}")

    if item.delivered_on is None:
        return _no("returns#window", "The order hasn't been delivered yet, so there's nothing to return.", None)

    days = (today - item.delivered_on).days
    is_ebook = item.format == "ebook"

    decision: Decision
    if reason in ("damaged", "wrong_item"):
        decision = Decision(True, "refund", item.price_cents, False, "returns#damaged",
                            "Damaged or wrong items are always refunded in full.", days)
    elif is_ebook and item.downloaded:
        return _no("ebooks#downloaded", "Ebooks can't be returned once they've been downloaded.", days)
    elif item.staff_pick and reason == "didnt_enjoy" and days <= GUARANTEE_WINDOW_DAYS:
        decision = Decision(True, "store_credit", item.price_cents, False, "guarantee#staff-picks",
                            "Staff picks carry the Bookseller's Guarantee: store credit within 60 days, "
                            "even if the book was read.", days)
    elif days <= RETURN_WINDOW_DAYS and (is_ebook or is_unread):
        # is_ebook here means not downloaded (downloaded ebooks were handled above).
        decision = Decision(True, "refund", item.price_cents, False, "returns#window",
                            "Unread books can be returned for a refund within 30 days of delivery.", days)
    else:
        if days > RETURN_WINDOW_DAYS:
            why = f"It was delivered {days} days ago, which is outside the 30-day return window."
        else:
            why = "Only unread books can be returned for a refund (staff picks have their own guarantee)."
        return _no("returns#window", why, days)

    over_cap = decision.amount_cents > approval_cap_cents
    too_many = prior_returns_90d >= max_returns_90d
    if over_cap or too_many:
        why = (f"Returns over ${approval_cap_cents / 100:.0f} need a bookseller's approval."
               if over_cap else "A 4th return within 90 days needs a bookseller's approval.")
        return Decision(True, decision.resolution, decision.amount_cents, True, "returns#approval",
                        f"{decision.reason} {why}", days)
    return decision
