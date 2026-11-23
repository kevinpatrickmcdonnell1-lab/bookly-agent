"""Router: rules first, then a Haiku classifier, then a pure decision function.

`classify()` calls the model. `decide()` is pure: given the classifier output and
the session, it picks the AOP, the write tools and the executor mode. That split
lets every routing rule (thresholds, sticky AOP, multi-intent, fail safe) be unit
tested without a key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from bookly.agent import gate
from bookly.agent.state import Session
from bookly.config import PKG, Settings

Intent = Literal["order_status", "return_refund", "policy_question", "account_access", "human_request",
                 "sensitive", "out_of_scope", "unclear"]
INTENTS: tuple[str, ...] = Intent.__args__  # type: ignore[attr-defined]

INTENT_TO_AOP = {
    "order_status": "order_status",
    "return_refund": "returns",
    "policy_question": "policy_qa",
    "account_access": "account_access",
    "out_of_scope": "general",
    "unclear": "general",
}
AOP_TO_INTENT = {v: k for k, v in INTENT_TO_AOP.items() if v != "general"}

ROUTER_PROMPT = (PKG / "prompts" / "router.md").read_text()


class Entities(BaseModel):
    order_number: str | None = None
    email: str | None = None
    item_hint: str | None = None


class RouterOutput(BaseModel):
    intents: list[Intent] = Field(default_factory=lambda: ["unclear"])
    confidence: float = 0.0
    entities: Entities = Field(default_factory=Entities)
    frustration: Literal["low", "medium", "high"] = "low"
    confirmation: Literal["yes", "no", "none"] = "none"
    escalate: bool = False
    reason: str = ""


_NULLABLE_STR = {"type": ["string", "null"]}
ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "intents": {"type": "array", "items": {"type": "string", "enum": list(INTENTS)}},
        "confidence": {"type": "number"},
        "entities": {
            "type": "object",
            "properties": {"order_number": _NULLABLE_STR, "email": _NULLABLE_STR, "item_hint": _NULLABLE_STR},
            "required": ["order_number", "email", "item_hint"],
            "additionalProperties": False,
        },
        "frustration": {"type": "string", "enum": ["low", "medium", "high"]},
        "confirmation": {"type": "string", "enum": ["yes", "no", "none"]},
        "escalate": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["intents", "confidence", "entities", "frustration", "confirmation", "escalate", "reason"],
    "additionalProperties": False,
}

# ---- rules (run before, and regardless of, the model) -----------------------

_HUMAN = re.compile(
    r"\b(?:(?:talk|speak|chat)\s+(?:to|with)\s+(?:a|an|the|some|your)?\s*"
    r"(?:human|person|real person|representative|rep|agent|bookseller|someone|somebody|manager)"
    r"|(?:want|need|get|give|connect|put|transfer)\b[^.?!]{0,25}\b(?:real|live|actual)\s+(?:person|human|agent)"
    r"|(?:get|give|want|need|transfer)\s+(?:me\s+)?(?:to\s+)?(?:a|an)?\s*(?:human|real person|representative|live agent)"
    r")\b",
    re.I,
)
_BARE_HUMAN = re.compile(r"^\s*(?:human|agent|representative|operator|person)[\s.!?]*$", re.I)
_ORDER = re.compile(r"(?<![\d$.])#?(\d{4})(?![\d.])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class RuleSignals:
    human_request: bool = False
    order_numbers: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)


def rule_signals(text: str) -> RuleSignals:
    return RuleSignals(
        human_request=bool(_HUMAN.search(text) or _BARE_HUMAN.match(text)),
        order_numbers=_ORDER.findall(text),
        emails=_EMAIL.findall(text),
    )


# ---- model call -----------------------------------------------------------------

def _text_of(content) -> str:
    return content if isinstance(content, str) else " ".join(
        b.get("text", "") for b in content if b.get("type") == "text")


_TAG = re.compile(r"</?customer_message>")


def _context_for_router(s: Session, text: str) -> str:
    last_assistant = ""
    for m in reversed(s.messages):
        if m["role"] == "assistant":
            last_assistant = _text_of(m["content"])
            break
    earlier = [_TAG.sub("", _text_of(m["content"])) for m in s.messages if m["role"] == "user"]
    earlier = [e for e in earlier if e.strip()][-2:]
    pending = s.pending_action.resolution if s.pending_action else None
    focus = s.focus or {}
    focus_attrs = " ".join(f'{k}="{v}"' for k, v in focus.items() if k in ("title", "order_number", "status"))
    rec = f'<recommended title="{s.recommended["title"]}"/>\n' if s.recommended else ""
    return (
        f"<conversation_state active_procedure=\"{s.active_aop or 'none'}\" "
        f"verified=\"{s.verified_customer_id is not None}\" pending_proposal=\"{pending or 'none'}\"/>\n"
        f"<in_focus {focus_attrs}/>\n" + rec +
        "".join(f"<earlier_customer_message>{e[:300]}</earlier_customer_message>\n" for e in earlier) +
        f"<last_assistant_message>{last_assistant[:800]}</last_assistant_message>\n"
        f"<customer_message>{text}</customer_message>"
    )


async def classify(llm, model: str, s: Session, text: str) -> tuple[RouterOutput, object]:
    data, call = await llm.structured(model=model, system=ROUTER_PROMPT, user=_context_for_router(s, text),
                                      schema=ROUTER_SCHEMA, max_tokens=512)
    try:
        return RouterOutput.model_validate(data), call
    except ValidationError as exc:
        raise ValueError(f"router output failed validation: {exc}") from exc


# ---- focus resolution (pure) ----------------------------------------------------

_WORDS = re.compile(r"[a-z0-9]+")
_FILLER = {"the", "a", "an", "of", "and", "book", "one", "that", "this", "it"}


def _title_words(title: str) -> set[str]:
    return set(_WORDS.findall(title.lower())) - _FILLER


def resolve_focus(hint: str | None, s: Session) -> dict | None:
    """Match the router's item_hint to something already discussed. Code, not the model,
    decides the match. The item sharing the most title words with the hint wins; if two
    different titles tie, there's no match, so the executor asks which one they mean."""
    if not hint:
        return None
    words = _title_words(hint)
    candidates = [*reversed(s.mentioned)]
    if s.recommended:
        candidates.append({**s.recommended, "source": "recommendation"})
    best: dict[str, tuple[int, dict]] = {}   # title -> (overlap, newest entry for that title)
    for c in candidates:
        title = c.get("title")
        overlap = len(words & _title_words(title)) if title else 0
        if overlap and title not in best:
            best[title] = (overlap, c)
    if not best:
        return None
    ranked = sorted(best.values(), key=lambda v: v[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def focus_note(out: RouterOutput | None, s: Session) -> str | None:
    """A note for the executor naming the item the customer is referring to."""
    if out is None:
        return None
    hit = resolve_focus(out.entities.item_hint, s)
    if hit is None:
        return None
    where = []
    if hit.get("order_number"):
        where.append(f"order {hit['order_number']}")
    if hit.get("status"):
        where.append(hit["status"])
    if hit.get("source") == "recommendation":
        where.append("the staff pick you recommended")
    detail = f" ({', '.join(where)})" if where else ""
    return (f"The customer is referring to {hit['title']}{detail}. Answer for that item; don't ask which one "
            "they mean.")


# ---- decision (pure) -----------------------------------------------------------

@dataclass
class RouteDecision:
    aop: str
    mode: str = "normal"                      # normal | clarify | offer_options | out_of_scope | fail_safe
    write_tools: frozenset[str] = frozenset()
    escalate: str | None = None               # None | "customer_request" | "sensitive_topic"
    queue: str = "booksellers"
    queued_intents: list[str] = field(default_factory=list)
    confirmation: str = "none"
    frustration: str = "low"
    notes: list[str] = field(default_factory=list)
    why: str = ""


def decide(out: RouterOutput | None, s: Session, rules: RuleSignals, settings: Settings) -> RouteDecision:
    # Rules first: an explicit request for a human is honoured at once, no persuasion.
    if rules.human_request:
        return RouteDecision(aop=s.active_aop or "general", escalate="customer_request",
                             frustration=out.frustration if out else "low",
                             why="rule: explicit request for a human")

    # Fail safe: router error or timeout -> general AOP, read-only tools.
    if out is None:
        return RouteDecision(aop="general", mode="fail_safe", why="router unavailable: read-only fail safe")

    intents = [i for i in dict.fromkeys(out.intents)] or ["unclear"]
    if "sensitive" in intents:
        return RouteDecision(aop=s.active_aop or "general", escalate="sensitive_topic", queue="trust_safety",
                             frustration=out.frustration, why="router: sensitive topic")
    if "human_request" in intents or out.escalate:
        return RouteDecision(aop=s.active_aop or "general", escalate="customer_request",
                             frustration=out.frustration, why="router: human request")

    actionable = [i for i in intents if i in INTENT_TO_AOP and i not in ("unclear", "out_of_scope")]
    primary = actionable[0] if actionable else intents[0]
    new_aop = INTENT_TO_AOP.get(primary, "general")
    confirmation = out.confirmation if s.pending_action else "none"

    # A reply to a pending return proposal always goes back to the returns AOP.
    if s.pending_action and confirmation != "none":
        queued = [i for i in actionable if INTENT_TO_AOP[i] != "returns"]
        return RouteDecision(aop="returns", write_tools=gate.write_tools_for(["return_refund", *queued]),
                             queued_intents=queued, confirmation=confirmation, frustration=out.frustration,
                             why=f"answering pending proposal (confirmation={confirmation})")

    # Sticky AOP: stay put unless a new intent arrives with high confidence.
    if s.active_aop and s.active_aop != "general":
        answering = confirmation != "none"
        weak_switch = new_aop != s.active_aop and out.confidence < settings.switch_aop_at
        if answering or weak_switch or primary in ("unclear",):
            aop = s.active_aop
            queued = [i for i in actionable if INTENT_TO_AOP[i] != aop]
            stay_intent = AOP_TO_INTENT.get(aop, primary)
            return RouteDecision(
                aop=aop, write_tools=gate.write_tools_for([stay_intent, *queued]), queued_intents=queued,
                confirmation=confirmation, frustration=out.frustration,
                why=f"sticky: stayed in {aop} (answering={answering}, confidence={out.confidence:.2f})")

    # Clarify below the threshold, or when the intent is unclear.
    if not actionable and primary == "out_of_scope" and out.confidence >= settings.clarify_below:
        return RouteDecision(aop="general", mode="out_of_scope", frustration=out.frustration, why="out of scope")
    if not actionable or out.confidence < settings.clarify_below:
        mode = "offer_options" if s.unclear_streak >= 1 else "clarify"
        return RouteDecision(aop="general", mode=mode, frustration=out.frustration,
                             why=f"unclear (confidence={out.confidence:.2f}, streak={s.unclear_streak})")

    queued = [i for i in actionable[1:] if INTENT_TO_AOP[i] != new_aop]
    # Topic switch mid-return: answer the new question, then resume.
    if s.active_aop == "returns" and new_aop != "returns" and (s.pending_action or s.eligibility):
        if "return_refund" not in queued:
            queued.append("return_refund")
    decision = RouteDecision(
        aop=new_aop, write_tools=gate.write_tools_for([primary, *queued]), queued_intents=queued,
        confirmation=confirmation, frustration=out.frustration,
        why=f"intent {primary} ({out.confidence:.2f})" + (f", queued {queued}" if queued else ""))
    if queued:
        decision.notes.append("The customer asked for more than one thing. Handle "
                              f"{primary.replace('_', ' ')} first, say you'll then help with "
                              f"{', '.join(q.replace('_', ' ') for q in queued)}, and do so.")
    return decision
