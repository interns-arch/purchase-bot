"""Asking a vendor ONE thing at a time (User, 13 Nov 2026).

    bot:    Sir ye part hai kya with pcs?  6N2Z19805A x1
    vendor: haan 1 pcs
    bot:    Sir, 6N2Z19805A ka MRP kitna hai?
    vendor: 3589
    bot:    Sir, discount kitna denge? (%)
    vendor: 10
    bot:    Sir, kitne din mein aa jayega?
    vendor: 3 din

Only then is the quote recorded and the line decided, so the Dealer Portal
gets the MRP, discount and days with the PO-MST. A step the vendor already
answered ("haan 1 pcs, MRP 3589, 10%, 3 din") is skipped.

Because each question asks for ONE thing, a bare number in the answer IS
that thing ("3589" after "MRP kitna hai?"). Anything still unclear is asked
again once, then left for the desk.

The state lives on the question (`AdvanceVendorQuery.step` / `.draft`), so
it survives restarts. A vendor who stops halfway: what he did say is kept
when his time runs out (`flush_partial`)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from backend.app.advance_orders import models as m
from backend.app.advance_orders.parser import LineAnswer, _DISC, _MRP, parse_eta, parse_vendor_reply
from core.logging_setup import get_logger

logger = get_logger(__name__)

PCS, MRP, DISCOUNT, ETA, DONE = "pcs", "mrp", "discount", "eta", "done"
_ORDER = (MRP, DISCOUNT, ETA)
_FIELD = {MRP: "mrp", DISCOUNT: "disc", ETA: "tat"}
_NUMBER = re.compile(r"(?<![A-Za-z0-9.])(\d[\d,]*(?:\.\d{1,3})?)(?![A-Za-z0-9])")
_PART_TOKEN = re.compile(r"[A-Za-z0-9-]{5,}")


def _key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _rows(lines: list[m.AdvanceOrderLine]) -> str:
    return "\n".join(f"{i}. {line.part_number} x{line.qty}" for i, line in enumerate(lines, start=1))


# ------------------------------------------------------------------ wording
def first_question(lines: list[m.AdvanceOrderLine]) -> str:
    if len(lines) == 1:
        return f"Sir ye part hai kya with pcs?\n\n{lines[0].part_number} x{lines[0].qty}"
    return f"Sir ye parts hai kya with pcs?\n\n{_rows(lines)}"


def question(step: str, lines: list[m.AdvanceOrderLine]) -> str:
    """The question for `step`, about the parts he said he HAS."""
    one = len(lines) == 1
    names = lines[0].part_number if one else None
    if step == MRP:
        if one:
            return f"Sir, {names} ka MRP kitna hai?"
        first = lines[0].part_number
        return f"Sir, har part ka MRP bata dijiye:\n{_rows(lines)}\n\nJaise: \"{first} 1200\""
    if step == DISCOUNT:
        if one:
            return f"Sir, {names} pe discount kitna denge? (%)"
        return f"Sir, in parts pe discount kitna denge? (%)\n{_rows(lines)}"
    if step == ETA:
        if one:
            return f"Sir, {names} kitne din mein aa jayega?"
        return f"Sir, ye parts kitne din mein aa jayenge?\n{_rows(lines)}"
    return ""


def reminder(step: str, lines: list[m.AdvanceOrderLine]) -> str:
    return "🔔 " + (first_question(lines) if step == PCS else question(step, lines))


# ------------------------------------------------------------------ reading
def _amount(text: str) -> Decimal | None:
    try:
        value = Decimal(str(text).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def _numbers(text: str, parts: list[str]) -> list[Decimal]:
    """Every number in `text`, part numbers removed first."""
    clean = text
    for part in parts:
        clean = re.sub(re.escape(part), " ", clean, flags=re.I)
    for token in _PART_TOKEN.findall(clean):
        if _key(token) in {_key(p) for p in parts}:
            clean = clean.replace(token, " ")
    return [v for v in (_amount(n) for n in _NUMBER.findall(clean)) if v is not None]


def _per_part(text: str, lines: list[m.AdvanceOrderLine]) -> tuple[dict[int, str], str]:
    """({line_id: the words written beside that part}, the rest)."""
    by_key = {_key(line.part_number): line.id for line in lines}
    said: dict[int, str] = {}
    rest: list[str] = []
    # A comma inside a number ("3,589") is not a separator.
    for chunk in re.split(r"[\n;]+|,(?!\d)", text):
        hit = None
        for token in _PART_TOKEN.findall(chunk):
            if _key(token) in by_key:
                hit = by_key[_key(token)]
                chunk = chunk.replace(token, " ")
                break
        if hit is None:
            rest.append(chunk)
        else:
            said[hit] = chunk
    return said, " ".join(rest)


def _read_mrp(text: str, parts: list[str]) -> Decimal | None:
    labelled = _MRP.search(text)
    if labelled:
        return _amount(labelled.group(1))
    found = _numbers(text, parts)
    return found[0] if len(found) == 1 else None


def _read_discount(text: str, parts: list[str]) -> Decimal | None:
    labelled = _DISC.search(text)
    if labelled:
        value = _amount(next(g for g in labelled.groups() if g))
    else:
        if re.search(r"\b(nahi|nhi|no|nil|zero)\b", text, re.I) and not _numbers(text, parts):
            return Decimal(0)  # "discount nahi" -- none, and that is an answer
        found = _numbers(text, parts)
        value = found[0] if len(found) == 1 else None
    return value if value is not None and 0 <= value <= 100 else None


def _read_days(text: str, parts: list[str], today: date) -> int | None:
    eta = parse_eta(text, today)
    if eta is not None and eta >= today:
        return (eta - today).days
    found = _numbers(text, parts)
    if len(found) == 1 and found[0] == int(found[0]) and 0 <= found[0] <= 120:
        return int(found[0])
    return None


def read(step: str, text: str, lines: list[m.AdvanceOrderLine], today: date) -> dict[int, object]:
    """{line_id: value} for the parts this answer covers."""
    parts = [line.part_number for line in lines]
    reader = {
        MRP: lambda t: _read_mrp(t, parts),
        DISCOUNT: lambda t: _read_discount(t, parts),
        ETA: lambda t: _read_days(t, parts, today),
    }[step]
    said, rest = _per_part(text, lines)
    out: dict[int, object] = {}
    for line_id, words in said.items():
        value = reader(words)
        if value is not None:
            out[line_id] = value
    if rest.strip():
        general = reader(rest)
        # One MRP for several parts cannot be right -- every part has its own.
        if general is not None and not (step == MRP and len(lines) > 1):
            for line in lines:
                out.setdefault(line.id, general)
        elif general is not None and len(lines) - len(out) == 1:
            missing = next(line for line in lines if line.id not in out)
            out[missing.id] = general
    return out


def read_pcs(text: str, lines: list[m.AdvanceOrderLine], today: date) -> dict[int, LineAnswer] | None:
    rows = [(line.id, line.part_number, line.qty) for line in lines]
    answers = parse_vendor_reply(text, rows, today)
    if not answers:
        return None
    if len(lines) == 1:
        answer = answers.get(lines[0].id)
        if answer is not None and answer.available and answer.available_qty is None:
            # "haan 1" after "with pcs?": the bare number is the pieces.
            found = [n for n in _numbers(text, [lines[0].part_number]) if n == int(n)]
            if answer.tat_days is None and len(found) == 1 and 0 < found[0] <= 100000:
                answer.available_qty = int(found[0])
    for line in lines:
        answer = answers.get(line.id)
        if answer is not None and answer.available_qty is not None and answer.available_qty > line.qty:
            answer.available_qty = line.qty
    return answers


# ------------------------------------------------------------------ state
def _entry(answer: LineAnswer) -> dict:
    return {
        "available": bool(answer.available),
        "qty": answer.available_qty,
        "mrp": str(answer.mrp) if answer.mrp is not None else None,
        "disc": str(answer.discount_pct) if answer.discount_pct is not None else None,
        "tat": answer.tat_days,
    }


def next_step(draft: dict, lines: list[m.AdvanceOrderLine]) -> str:
    have = [line for line in lines if (draft.get(str(line.id)) or {}).get("available")]
    for step in _ORDER:
        if any((draft.get(str(line.id)) or {}).get(_FIELD[step]) is None for line in have):
            return step
    return DONE


def answer_for(entry: dict, today: date) -> LineAnswer:
    tat = entry.get("tat")
    return LineAnswer(
        available=bool(entry.get("available")),
        available_qty=entry.get("qty"),
        tat_days=tat,
        eta=today + timedelta(days=int(tat)) if tat is not None else None,
        mrp=Decimal(entry["mrp"]) if entry.get("mrp") is not None else None,
        discount_pct=Decimal(entry["disc"]) if entry.get("disc") is not None else None,
    )


def start(q: m.AdvanceVendorQuery) -> None:
    q.step, q.draft = PCS, {}


def advance_draft(q: m.AdvanceVendorQuery, text: str, lines: list[m.AdvanceOrderLine], today: date) -> bool:
    """Read the answer to the current step into `q.draft`. False = unreadable."""
    draft = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (q.draft or {}).items()}
    if q.step == PCS:
        answers = read_pcs(text, lines, today)
        if not answers:
            return False
        for line_id, answer in answers.items():
            if not answer.ambiguous:
                draft[str(line_id)] = _entry(answer)
        if not any(str(line.id) in draft for line in lines):
            return False
        # A part he did not mention when he named others: not available from him.
        for line in lines:
            draft.setdefault(str(line.id), {"available": False})
    else:
        have = [line for line in lines if (draft.get(str(line.id)) or {}).get("available")]
        values = read(q.step, text, have, today)
        if not values:
            return False
        for line_id, value in values.items():
            draft[str(line_id)][_FIELD[q.step]] = str(value) if isinstance(value, Decimal) else value
    q.draft = draft
    return True
