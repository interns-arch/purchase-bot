"""Reading a vendor's WhatsApp reply to an advance-order question.

Vendors answer the way people do:

    "haan"                          -> all asked parts available, no date
    "5 din"  /  "20 sep tak"        -> all available, with that ETA
    "nahi hai"                      -> none available
    "16510M68K10 hai 3 din, 2630002752 nahi"
                                    -> per part
    "sirf 4 milenge, 10 din"        -> available, only 4, ETA 10 days

Deterministic on purpose: an ETA promised to a customer must come from words
the vendor actually wrote. A reply this cannot read returns None, and the
caller forwards it to the admins instead of guessing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_WORD_NUM = {"ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
             "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

_NO = re.compile(
    r"\b(nahi|nahin|nhi|no|not\s+available|n/?a|nil|out\s+of\s+stock|khatam|nai|nahi\s+hai|nhi\s+hai|nahi\s+milega|nahi\s+milenge)\b",
    re.I,
)
_YES = re.compile(
    r"\b(haan|han|haa|ha|hn|yes|ok|okay|available|avl|avail|hai|h|ready|mil\s+jayega|mil\s+jaega|milega|milenge|ho\s+jayega|ho\s+jaega|aa\s+jayega|aa\s+jaega|bhej\s+denge|de\s+denge|stock\s+(mein|me|main))\b",
    re.I,
)
_NOW = re.compile(r"\b(aaj|today|ready|ready\s+stock|stock\s+(mein|me|main)\s+hai|abhi\s+hai|turant)\b", re.I)
_ONLY_QTY = re.compile(r"\b(?:sirf|only|bas|keval)\s*(\d{1,5})\b|\b(\d{1,5})\s*(?:pcs|pc|nos|no\.?s?|piece|pieces|qty)\b", re.I)


@dataclass
class LineAnswer:
    available: bool
    available_qty: int | None = None
    eta: date | None = None


def _key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _mkdate(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_eta(text: str, today: date) -> date | None:
    s = str(text or "").lower()
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s*,?\s*(\d{2,4}))?\b", s)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        year = year + 2000 if year < 100 else year
        d = _mkdate(year, _MONTHS.index(m.group(2)) + 1, int(m.group(1)))
        if d and not m.group(3) and d < today - timedelta(days=60):
            d = _mkdate(year + 1, d.month, d.day)
        return d
    m = re.search(r"(?:^|[^\d])(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?(?!\d)", s)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        year = year + 2000 if year < 100 else year
        return _mkdate(year, int(m.group(2)), int(m.group(1)))

    def n(v: str) -> int:
        return int(v) if v.isdigit() else _WORD_NUM.get(v, 0)

    m = re.search(r"\b(\d{1,3}|ek|do|teen|tin|char|chaar|paanch|panch|one|two|three|four|five)\s*(din|dino|dinon|day|days)\b", s)
    if m and n(m.group(1)):
        return today + timedelta(days=n(m.group(1)))
    m = re.search(r"\b(\d{1,2}|ek|do|teen|one|two|three)\s*(hafte|hafta|haftey|week|weeks)\b", s)
    if m and n(m.group(1)):
        return today + timedelta(days=7 * n(m.group(1)))
    if re.search(r"\b(parso|parson)\b", s):
        return today + timedelta(days=2)
    if re.search(r"\b(kal|tomorrow)\b", s):
        return today + timedelta(days=1)
    if _NOW.search(s):
        return today
    return None


def _answer(chunk: str, today: date) -> LineAnswer | None:
    text = chunk.strip()
    if not text:
        return None
    eta = parse_eta(text, today)
    qty_match = _ONLY_QTY.search(text)
    qty = int(qty_match.group(1) or qty_match.group(2)) if qty_match else None
    if _NO.search(text) and eta is None and not qty:
        return LineAnswer(available=False)
    if eta is not None or qty or _YES.search(text):
        if qty == 0:
            return LineAnswer(available=False)
        return LineAnswer(available=True, available_qty=qty, eta=eta)
    return None


def parse_vendor_reply(text: str, lines: list[tuple[int, str, int]], today: date) -> dict[int, LineAnswer] | None:
    """`lines` = [(line_id, part_number, qty)] that were asked about.
    Returns {line_id: LineAnswer} for the lines the reply answers, or None
    when it answers none of them."""
    body = str(text or "").strip()
    if not body or not lines:
        return None
    by_key = {_key(part): line_id for line_id, part, _qty in lines}
    specific: dict[int, LineAnswer] = {}
    general: list[str] = []
    for chunk in re.split(r"[\n;,]+", body):
        hit = None
        for token in re.findall(r"[A-Za-z0-9-]{5,}", chunk):
            if _key(token) in by_key:
                hit = by_key[_key(token)]
                chunk = chunk.replace(token, " ")
                break
        if hit is None:
            general.append(chunk)
            continue
        ans = _answer(chunk, today)
        if ans is not None:
            specific[hit] = ans

    out = dict(specific)
    if len(lines) == 1 and not specific:
        ans = _answer(" ".join(general), today)
        if ans is not None:
            out[lines[0][0]] = ans
    elif general:
        ans = _answer(" ".join(general), today)
        if ans is not None:
            for line_id, _part, _qty in lines:
                out.setdefault(line_id, ans)
    return out or None
