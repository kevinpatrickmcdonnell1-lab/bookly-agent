"""Input guard (rules + Haiku, in parallel with the router) and output guard
(code checks first, a Haiku supervisor only when a reply states a policy)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from bookly.agent.state import Session
from bookly.config import PKG

GUARD_PROMPT = (PKG / "prompts" / "input_guard.md").read_text()
SUPERVISOR_PROMPT = (PKG / "prompts" / "supervisor.md").read_text()

# ---- input guard ----------------------------------------------------------------

_INJECTION = re.compile(
    r"ignore (?:all |any |your |the |previous |prior )*(?:rules|instructions|polic(?:y|ies)|prompt)"
    r"|disregard (?:all |your |the |previous )*(?:rules|instructions)"
    r"|you are now\b|developer mode|system prompt|\bsystem:|\[system\]|new instructions"
    r"|pretend (?:you are|to be)|act as (?:an? )?(?:admin|developer|manager)",
    re.I,
)
_LEGAL = re.compile(r"\b(lawyer|attorney|sue|suing|lawsuit|legal action|small claims)\b", re.I)
_CHARGEBACK = re.compile(r"\b(chargeback|charge back|dispute (?:the|this|it) (?:charge|with my bank)|call my bank|"
                         r"report (?:you|this) to my (?:bank|card))\b", re.I)
_SAFETY = re.compile(r"\b(kill myself|hurt myself|suicid\w*|self[- ]harm)\b", re.I)


class GuardOutput(BaseModel):
    injection: bool = False
    abuse: bool = False
    sensitive: Literal["none", "legal", "chargeback", "safety", "harassment"] = "none"
    reason: str = ""


GUARD_SCHEMA = {
    "type": "object",
    "properties": {
        "injection": {"type": "boolean"},
        "abuse": {"type": "boolean"},
        "sensitive": {"type": "string", "enum": ["none", "legal", "chargeback", "safety", "harassment"]},
        "reason": {"type": "string"},
    },
    "required": ["injection", "abuse", "sensitive", "reason"],
    "additionalProperties": False,
}


def guard_rules(text: str) -> GuardOutput:
    sensitive = "none"
    if _SAFETY.search(text):
        sensitive = "safety"
    elif _LEGAL.search(text):
        sensitive = "legal"
    elif _CHARGEBACK.search(text):
        sensitive = "chargeback"
    injection = bool(_INJECTION.search(text))
    reasons = []
    if injection:
        reasons.append("rule: instruction-override phrasing")
    if sensitive != "none":
        reasons.append(f"rule: {sensitive} keywords")
    return GuardOutput(injection=injection, sensitive=sensitive, reason="; ".join(reasons))


def combine(rules: GuardOutput, model: GuardOutput | None) -> GuardOutput:
    if model is None:
        return rules
    return GuardOutput(
        injection=rules.injection or model.injection,
        abuse=model.abuse,
        sensitive=rules.sensitive if rules.sensitive != "none" else model.sensitive,
        reason="; ".join(r for r in (rules.reason, model.reason) if r),
    )


async def input_guard_model(llm, model: str, text: str) -> tuple[GuardOutput, object]:
    data, call = await llm.structured(model=model, system=GUARD_PROMPT,
                                      user=f"<customer_message>{text}</customer_message>",
                                      schema=GUARD_SCHEMA, max_tokens=256)
    return GuardOutput.model_validate(data), call


# ---- output guard -----------------------------------------------------------------

# Facts are normalised before comparison, so "$25", "25.00 dollars" and "25 USD" are the same amount and
# "November 30", "Nov 30th", "30 Nov" and "11/30" are the same date. A fact the model phrases differently
# from the tools is still a fact, and still has to be in a tool result.
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?"
_MONEY = re.compile(
    rf"\$\s?{_NUM}"                                         # $18.99
    rf"|\b{_NUM}\s?(?:dollars?|bucks|usd)\b"                # 18.99 dollars, 25 USD
    rf"|\busd\s?{_NUM}",                                    # USD 25
    re.I,
)
_MONTH_NUM = {name: i for i, names in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
    ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"), ("october", "oct"),
    ("november", "nov"), ("december", "dec"),
), start=1) for name in names}
# "May" only when capitalised, so "2 may arrive separately" isn't read as a date.
_MON = "|".join("(?-i:May)" if m == "may" else m for m in sorted(_MONTH_NUM, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"
_DATE = re.compile(
    rf"\b(?P<m1>{_MON})\.?\s+(?:the\s+)?(?P<d1>\d{{1,2}}){_ORD}\b"           # November 30, Nov. 30th
    rf"|\b(?:the\s+)?(?P<d2>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<m2>{_MON})\b"   # 30 November, the 30th of Nov
    rf"|\b\d{{4}}-(?P<m3>\d{{2}})-(?P<d3>\d{{2}})(?!\d)"                         # 2026-11-30
    rf"|(?<![\d/])(?P<m4>\d{{1,2}})/(?P<d4>\d{{1,2}})(?:/\d{{2,4}})?(?![\d/])",  # 11/30, 11/30/2026
    re.I,
)
# No tool returns a relative date, so one in a reply is unbacked unless the phrase itself is in a tool result
# (the book "Tomorrow, and Tomorrow, and Tomorrow" is).
_RELATIVE_DATE = re.compile(
    r"\b(?:the day after tomorrow|tomorrow|tonight|(?:this|next|by|on)\s+(?:week(?:end)?|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|end of (?:the|this) week|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_TRACKING = re.compile(r"\b(1Z[0-9A-Z]{10,}|\d{12,22})\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_POLICY_CLAIM = re.compile(
    r"\b(\d+)[- ]days?\b|\bpolicy\b|\bguarantee\b|\bwindow\b|\brefundable\b|\bnon-refundable\b|"
    r"\bcan(?:'|no)t be (?:returned|refunded)\b|\bstore credit\b|\bwe (?:only )?ship\b|\bfree shipping\b|"
    r"\bnever expires\b|\bbusiness days\b",
    re.I,
)
_POLICY_ID = re.compile(r"\b[a-z][a-z-]*#[a-z][a-z-]*\b")
_MARKDOWN = re.compile(r"\*\*|__|^#{1,6}\s|^\s*[-*]\s", re.M)


def _cents(m: re.Match) -> int:
    whole, frac = next((m.group(i), m.group(i + 1)) for i in (1, 3, 5) if m.group(i))
    return int(whole.replace(",", "")) * 100 + int(frac or 0)


def _dates(text: str) -> list[tuple[str, tuple[int, int]]]:
    """Every date in text as (as written, (month, day)). Things that can't be dates, like 24/7, are skipped."""
    out = []
    for m in _DATE.finditer(text):
        g = m.groupdict()
        if g["m1"] or g["m2"]:
            month, day = _MONTH_NUM[(g["m1"] or g["m2"]).lower()], int(g["d1"] or g["d2"])
        else:
            month, day = int(g["m3"] or g["m4"]), int(g["d3"] or g["d4"])
        if 1 <= month <= 12 and 1 <= day <= 31:
            out.append((m.group(0).strip(), (month, day)))
    return out


@dataclass
class OutputCheck:
    ok: bool
    issues: list[str] = field(default_factory=list)
    needs_supervisor: bool = False
    checked: dict = field(default_factory=dict)


def code_checks(reply: str, s: Session, customer_texts: list[str]) -> OutputCheck:
    corpus = " ".join(s.grounding) + " " + " ".join(s.policy_texts.values())
    issues: list[str] = []

    known_cents = {_cents(m) for m in _MONEY.finditer(corpus)}
    amounts = [m.group(0) for m in _MONEY.finditer(reply)]
    for m in _MONEY.finditer(reply):
        if _cents(m) not in known_cents:
            issues.append(f"amount {m.group(0)} isn't in any tool result")

    known_dates = {md for _, md in _dates(corpus)}
    dates = _dates(reply)
    for written, md in dates:
        if md not in known_dates:
            issues.append(f"date {written} isn't in any tool result")

    corpus_lower = corpus.lower()
    relative = [m.group(0) for m in _RELATIVE_DATE.finditer(reply) if m.group(0).lower() not in corpus_lower]
    for r in relative:
        issues.append(f"'{r}' is a relative date no tool returned; give the date from the tool result instead")

    tracking = _TRACKING.findall(reply)
    for t in tracking:
        if t not in corpus:
            issues.append(f"tracking number {t} isn't in any tool result")

    typed = " ".join(customer_texts).lower()
    for e in _EMAIL.findall(reply):
        if e.lower() not in typed:
            issues.append("reply contains a full email address the customer didn't give")

    if _MARKDOWN.search(reply):
        issues.append("reply uses markdown; write plain text")

    if _POLICY_ID.search(reply):
        issues.append("reply shows an internal policy id; name the policy in plain words instead")

    return OutputCheck(ok=not issues, issues=issues, needs_supervisor=bool(_POLICY_CLAIM.search(reply)),
                       checked={"amounts": amounts, "dates": [w for w, _ in dates] + relative, "tracking": tracking})


class SupervisorOutput(BaseModel):
    grounded: bool
    unsupported: list[str] = []


SUPERVISOR_SCHEMA = {
    "type": "object",
    "properties": {"grounded": {"type": "boolean"}, "unsupported": {"type": "array", "items": {"type": "string"}}},
    "required": ["grounded", "unsupported"],
    "additionalProperties": False,
}


async def supervise(llm, model: str, reply: str, s: Session, kb) -> tuple[SupervisorOutput, object]:
    texts = dict(s.policy_texts)
    for ref in s.policy_refs:
        if ref in kb.by_id:
            texts.setdefault(ref, kb.by_id[ref].text)
    facts = "\n".join(f"[{k}] {v}" for k, v in texts.items()) or "(none)"
    tool_results = "\n".join(s.grounding[-8:]) or "(none)"
    user = (f"<policy_texts>\n{facts}\n</policy_texts>\n<tool_results>\n{tool_results}\n</tool_results>\n"
            f"<draft_reply>{reply}</draft_reply>")
    data, call = await llm.structured(model=model, system=SUPERVISOR_PROMPT, user=user, schema=SUPERVISOR_SCHEMA,
                                      max_tokens=512)
    return SupervisorOutput.model_validate(data), call
