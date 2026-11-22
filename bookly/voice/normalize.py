"""Turn a speech transcript into the text a customer would have typed.

The router finds order numbers with a 4-digit pattern and verification needs a written email, so
"forty-eight oh one" and "dev dot shah at example dot com" have to become "4801" and
"dev.shah@example.com" before the turn runs. Everything else is left exactly as transcribed."""

from __future__ import annotations

import re

_UNITS = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 2, "thirty": 3, "forty": 4, "fifty": 5, "sixty": 6, "seventy": 7, "eighty": 8, "ninety": 9}
_REPEAT = {"double": 2, "triple": 3}

_unit = "|".join(_UNITS)
_tok = (rf"(?:\d+|(?:{'|'.join(_TENS)})(?:[- ](?:{_unit}))?|{'|'.join(_TEENS)}|{_unit}"
        rf"|(?:{'|'.join(_REPEAT)})\s+(?:{_unit}|\d))")
_TOKEN = re.compile(rf"\b{_tok}\b", re.I)
# Two or more number tokens in a row, separated by spaces, hyphens or commas.
_RUN = re.compile(rf"(?<![\d$.,])\b{_tok}(?:(?:\s*,\s*|\s+|-)\b{_tok}\b)+(?![\d.]|,\d)", re.I)


def _digits(token: str) -> str:
    t = token.lower()
    if t.isdigit():
        return t
    parts = re.split(r"[- ]+", t)
    if parts[0] in _REPEAT:
        d = parts[1] if parts[1].isdigit() else str(_UNITS[parts[1]])
        return d * _REPEAT[parts[0]]
    if parts[0] in _TENS:
        return f"{_TENS[parts[0]]}{_UNITS[parts[1]] if len(parts) > 1 else 0}"
    if t in _TEENS:
        return str(_TEENS[t])
    return str(_UNITS[t])


def _join_numbers(text: str) -> str:
    def repl(m: re.Match) -> str:
        joined = "".join(_digits(t) for t in _TOKEN.findall(m.group(0)))
        # Only long runs are identifiers (order numbers, tracking). "one or two" and "2 3" stay as said.
        return joined if len(joined) >= 4 else m.group(0)
    return _RUN.sub(repl, text)


_SPOKEN_SEP = {"dot": ".", "period": ".", "underscore": "_", "dash": "-", "hyphen": "-", "plus": "+"}
_sep = "|".join(_SPOKEN_SEP)
_part = r"[a-z0-9][\w+-]*"
_LOCAL = rf"{_part}(?:(?:\s+(?:{_sep})\s+|\.){_part})*"
_DOMAIN = rf"{_part}(?:(?:\s+(?:dot|period)\s+|\.){_part})+"
_SPOKEN_EMAIL = re.compile(rf"\b({_LOCAL})\s+(?:at|@)\s+({_DOMAIN})\b", re.I)
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}$", re.I)
_SEP_WORD = re.compile(rf"\s+({_sep})\s+", re.I)


def _join_emails(text: str) -> str:
    def repl(m: re.Match) -> str:
        local = _SEP_WORD.sub(lambda w: _SPOKEN_SEP[w.group(1).lower()], m.group(1))
        domain = _SEP_WORD.sub(lambda w: _SPOKEN_SEP[w.group(1).lower()], m.group(2))
        email = f"{local}@{domain}".lower()
        # "at" only becomes "@" when the domain was really spoken ("example dot com"), so
        # "I'm at home" and "look at the order" are untouched.
        spoken_domain = re.search(r"\s(?:dot|period)\s", m.group(2), re.I) or "." in m.group(2)
        return email if spoken_domain and _EMAIL.match(email) else m.group(0)
    return _SPOKEN_EMAIL.sub(repl, text)


def normalize_transcript(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return _join_emails(_join_numbers(text))
