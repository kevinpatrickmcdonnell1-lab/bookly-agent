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

_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December")
_DATE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
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
    return int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)


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

    known_dates = {(mo, int(d)) for mo, d in _DATE.findall(corpus)}
    dates = _DATE.findall(reply)
    for mo, d in dates:
        if (mo, int(d)) not in known_dates:
            issues.append(f"date {mo} {d} isn't in any tool result")

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
                       checked={"amounts": amounts, "dates": [f"{m} {d}" for m, d in dates], "tracking": tracking})


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
