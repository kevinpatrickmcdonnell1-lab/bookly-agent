"""Rewrite an approved reply so it sounds right read aloud. It runs after the output guards and
only changes formatting: it never adds, drops or reorders a digit (tests/test_voice.py checks this),
so it can't introduce a fact the guards didn't see."""

from __future__ import annotations

import re

from bookly.agent.guards import _TRACKING

_MASKED_EMAIL = re.compile(r"\b([\w])•+@([\w-]+(?:\.[\w-]+)+)")
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_ORDER = re.compile(r"(\border\s+)?#(\d{2})(\d{2})\b", re.I)


def _say_domain(domain: str) -> str:
    return domain.replace(".", " dot ")


def _say_tracking(m: re.Match) -> str:
    code = m.group(0)
    groups = [code[i:i + 4] for i in range(0, len(code), 4)]
    return ", ".join(" ".join(g) for g in groups)


def speakable(reply: str) -> str:
    out = _MASKED_EMAIL.sub(lambda m: f"the address starting with {m.group(1)}, at {_say_domain(m.group(2))}", reply)
    out = _EMAIL.sub(lambda m: m.group(0).split("@")[0].replace(".", " dot ") + " at " + _say_domain(m.group(1)), out)
    out = _TRACKING.sub(_say_tracking, out)
    out = _ORDER.sub(lambda m: f"{m.group(1) or 'order '}number {m.group(2)} {m.group(3)}", out)
    return out
