"""Keyword (BM25) search over the policy docs in data/kb/.

Each `## Heading {#anchor}` section is one searchable unit with id `<doc>#<anchor>`.
Those ids are the same `policy_ref` values the policy engine returns.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from bookly.agent.state import Session
from bookly.config import PKG
from bookly.tools.common import ToolDeps

KB_DIR = PKG / "data" / "kb"
_HEADING = re.compile(r"^##\s+(.+?)\s*\{#([a-z0-9-]+)\}\s*$")
_WORD = re.compile(r"[a-z0-9$]+")
_STOP = set("a an and are as at be but by can do does for from how i if in is it my of on or our so that the "
            "to we what when with you your".split())


def tokenize(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.append(w)
    return out


@dataclass
class Section:
    id: str
    title: str
    text: str
    tokens: list[str]


class PolicyIndex:
    def __init__(self, kb_dir: Path = KB_DIR, k1: float = 1.5, b: float = 0.75):
        self.sections: list[Section] = []
        for path in sorted(kb_dir.glob("*.md")):
            self.sections.extend(self._parse(path))
        self.by_id = {s.id: s for s in self.sections}
        self.k1, self.b = k1, b
        self.avgdl = sum(len(s.tokens) for s in self.sections) / max(1, len(self.sections))
        df: Counter[str] = Counter()
        for s in self.sections:
            df.update(set(s.tokens))
        n = len(self.sections)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @staticmethod
    def _parse(path: Path) -> list[Section]:
        doc = path.stem
        out: list[Section] = []
        title, anchor, buf = None, None, []
        doc_title = ""
        for line in path.read_text().splitlines():
            if line.startswith("# "):
                doc_title = line[2:].strip()
                continue
            m = _HEADING.match(line)
            if m:
                if anchor:
                    out.append(_section(doc, anchor, f"{doc_title}: {title}", buf))
                title, anchor, buf = m.group(1), m.group(2), []
            elif anchor:
                buf.append(line)
        if anchor:
            out.append(_section(doc, anchor, f"{doc_title}: {title}", buf))
        return out

    def search(self, query: str, k: int = 3) -> list[Section]:
        q = tokenize(query)
        scored = []
        for s in self.sections:
            tf = Counter(s.tokens)
            score = 0.0
            for t in q:
                if t not in tf:
                    continue
                f = tf[t]
                score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(s.tokens) / self.avgdl))
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:k]]


def _section(doc: str, anchor: str, title: str, lines: list[str]) -> Section:
    text = " ".join(l.strip() for l in lines if l.strip())
    return Section(f"{doc}#{anchor}", title, text, tokenize(title + " " + text))


def search_policy(s: Session, args: dict, deps: ToolDeps) -> dict:
    hits = deps.kb.search(str(args.get("query", "")), k=3)
    for h in hits:
        s.policy_texts[h.id] = h.text
    if not hits:
        return {"results": [], "note": "No policy matches. Don't guess; offer to connect a bookseller."}
    return {"results": [{"policy_ref": h.id, "title": h.title, "text": h.text} for h in hits],
            "note": "Answer only from these texts. Name the policy in plain words; never show the policy_ref id "
                    "to the customer."}
