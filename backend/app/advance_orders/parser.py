"""Reading a vendor's WhatsApp reply to an advance-order question.

Vendors answer the way people do:

    "haan"                          -> all asked parts available, no date
    "5 din"  /  "20 sep tak"        -> all available, with that ETA
    "nahi hai"                      -> none available
    "16510M68K10 hai 3 din, 2630002752 nahi"
                                    -> per part
    "sirf 4 milenge, 10 din"        -> available, only 4, ETA 10 days

    "16510M68K10 rate 450, 3 din"    -> available, rate 450, TAT 3 days

Deterministic on purpose: an ETA promised to a customer must come from words
the vendor actually wrote. A reply this cannot read returns None, and the
caller forwards it to the admins instead of guessing.

MONEY IS NEVER INFERRED
-----------------------
A number only becomes a price when the vendor LABELLED it one -- "rate 450",
"450 rs", "@450", "Rs.450", "450/-", "mrp 600". A bare number is not a price,
because in these replies a bare number is far more often a quantity or a
count of days: "5 din", "sirf 4", "10 pcs". This is the same rule the
spreadsheet importer already enforces from the other direction, where a money
column can never become a quantity (`core/ingestion/column_detector.py`).

When a rate was ASKED FOR and the reply carries an unlabelled number that
could be the rate, `ambiguous` is set and the caller hands the raw text to
the admins rather than quoting a guessed price to a customer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

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
# "samajh nahi aaya", "kya bhej rahe ho" -- a vendor asking US something.
# It contains "nahi", but reading it as "not available" would mark good parts
# dead. These go to a human instead.
_CONFUSED = re.compile(
    r"\b(samajh|samjha|matlab|kya\s+(hai|bhej|bol)|kaun\s?sa|konsa|repeat|phir\s+se|dobara|clarify|which\s+part|what\s+do\s+you\s+mean)\b",
    re.I,
)
_NOW = re.compile(r"\b(aaj|today|ready|ready\s+stock|stock\s+(mein|me|main)\s+hai|abhi\s+hai|turant)\b", re.I)
_ONLY_QTY = re.compile(r"\b(?:sirf|only|bas|keval)\s*(\d{1,5})\b|\b(\d{1,5})\s*(?:pcs|pc|nos|no\.?s?|piece|pieces|qty)\b", re.I)

_AMOUNT = r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
# MRP is always explicitly labelled -- it is read (and removed) FIRST, so that
# "mrp 600 rate 450" can never make 600 the rate.
_MRP = re.compile(r"\bmrp\s*:?\s*(?:\u20b9|rs\.?|inr)?\s*" + _AMOUNT, re.I)
# A number becomes a RATE only with a money word attached, on either side.
_RATE_BEFORE = re.compile(
    r"(?:\u20b9|\brs\.?|\binr\b|\brate\b|\bprice\b|\bdaam\b|\bbhav\b|@)\s*:?\s*" + _AMOUNT, re.I
)
_RATE_AFTER = re.compile(
    _AMOUNT + r"\s*(?:\u20b9|rs\.?|rupees?|rupaye|/-|each|per\s*pc|per\s*piece)", re.I
)
# Any standalone number. Used ONLY to notice that an unlabelled figure exists
# when a rate was expected -- never read as a value.
_ANY_NUMBER = re.compile(r"(?<![A-Za-z0-9.])([0-9][0-9,]*(?:\.[0-9]{1,2})?)(?![A-Za-z0-9])")


def _amount(text: str) -> Decimal | None:
    """A positive money figure, or None. Never raises on vendor input."""
    try:
        value = Decimal(str(text).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


@dataclass
class LineAnswer:
    available: bool
    available_qty: int | None = None
    eta: date | None = None
    tat_days: int | None = None
    quoted_rate: Decimal | None = None
    mrp: Decimal | None = None
    # True when a number was present that MIGHT be the rate but was not
    # labelled as one. The caller must not use this answer's price; it sends
    # the raw reply to the admins instead.
    ambiguous: bool = False


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


# Everything the reader has already UNDERSTOOD is blanked out of a chunk
# before it looks for stray numbers. What survives is a figure nobody has
# accounted for -- the only thing that can make a rate reply ambiguous.
_CONSUMED = [
    _MRP,
    _RATE_BEFORE,
    _RATE_AFTER,
    _ONLY_QTY,
    re.compile(r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s*,?\s*\d{2,4})?", re.I),
    re.compile(r"(?<!\d)\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?(?!\d)"),
    re.compile(r"\b\d{1,3}\s*(?:din|dino|dinon|day|days|hafte|hafta|haftey|week|weeks)\b", re.I),
]


def _residual_numbers(text: str) -> list[str]:
    residue = text
    for pattern in _CONSUMED:
        residue = pattern.sub(" ", residue)
    return _ANY_NUMBER.findall(residue)


def _answer(chunk: str, today: date, want_rate: bool = False) -> LineAnswer | None:
    text = chunk.strip()
    if not text:
        return None
    if _CONFUSED.search(text):
        return None  # a question back to us -- never an answer

    # MRP first, and removed, so "mrp 600 rate 450" cannot read 600 as the rate.
    mrp = None
    mrp_match = _MRP.search(text)
    rate_text = text
    if mrp_match:
        mrp = _amount(mrp_match.group(1))
        rate_text = text[: mrp_match.start()] + " " + text[mrp_match.end() :]

    rate = None
    rate_match = _RATE_BEFORE.search(rate_text) or _RATE_AFTER.search(rate_text)
    if rate_match:
        rate = _amount(rate_match.group(1))

    eta = parse_eta(text, today)
    qty_match = _ONLY_QTY.search(text)
    qty = int(qty_match.group(1) or qty_match.group(2)) if qty_match else None

    if _NO.search(text) and eta is None and not qty and rate is None:
        return LineAnswer(available=False)

    if eta is None and qty is None and rate is None and mrp is None and not _YES.search(text):
        return None
    if qty == 0:
        return LineAnswer(available=False)

    # A rate was asked for and none was labelled. If an unexplained number is
    # sitting in the reply it may well BE the rate, and guessing it would put
    # an invented price in front of a customer -- so the whole answer is
    # refused and a human reads the original words.
    if want_rate and rate is None and _residual_numbers(text):
        return LineAnswer(available=True, ambiguous=True)

    tat = (eta - today).days if eta is not None else None
    if tat is not None and tat < 0:
        # "20 sep" said on 29 Sep. Which year the vendor meant is a guess, and
        # a date already past cannot be promised to a customer -- so the part
        # stays AVAILABLE and simply carries no ETA. The desk sees the gap.
        tat, eta = None, None
    return LineAnswer(
        available=True,
        available_qty=qty,
        eta=eta,
        tat_days=tat,
        quoted_rate=rate,
        mrp=mrp,
    )


def parse_vendor_reply(
    text: str, lines: list[tuple[int, str, int]], today: date, want_rate: bool = False
) -> dict[int, LineAnswer] | None:
    """`lines` = [(line_id, part_number, qty)] that were asked about.
    `want_rate` is True for a vendor who has no standing discount and was
    therefore asked for a rate; it makes an unlabelled number ambiguous
    instead of ignorable.

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
        ans = _answer(chunk, today, want_rate)
        if ans is not None:
            specific[hit] = ans

    out = dict(specific)
    general_answer = _answer(" ".join(general), today, want_rate) if general else None

    # A RATE NOT WRITTEN BESIDE A PART NUMBER, while several parts were asked.
    # Different parts have different rates, so "rate 450" cannot be applied to
    # all of them, and it cannot be pinned to one either unless the vendor
    # named exactly one part. Everything else is refused as ambiguous and the
    # admin reads the vendor's words. A clear "nahi" on a named part stands.
    if (
        want_rate
        and general_answer is not None
        and general_answer.quoted_rate is not None
        and len(lines) > 1
    ):
        # Only parts he said YES to can carry a rate -- a refused part needs
        # none. So "TT-100 hai, TT-200 nahi, rate 450" is TT-100's rate.
        said_yes = [answer for answer in specific.values() if answer.available]
        if len(said_yes) == 1:
            _merge_into(said_yes[0], general_answer)
        else:
            for line_id, _part, _qty in lines:
                existing = out.get(line_id)
                if existing is not None and (not existing.available or existing.quoted_rate is not None):
                    continue  # a clear refusal, or a rate of its own
                out[line_id] = LineAnswer(available=True, ambiguous=True)
        return out or None

    if general_answer is not None:
        for line_id, _part, _qty in lines:
            existing = out.get(line_id)
            if existing is None:
                # Nothing part-specific was said about this line, so the
                # general part of the reply speaks for it.
                out[line_id] = general_answer
            else:
                # Something WAS said about this line. Vendors routinely split
                # one answer across commas -- "16510M68K10 rate 450, 3 din" --
                # so the general remainder fills the gaps it left. Availability
                # is never merged: a part-specific "nahi" must survive a
                # general "haan".
                _merge_into(existing, general_answer)

    if len(lines) == 1 and not out and general_answer is not None:
        out[lines[0][0]] = general_answer
    return out or None


def _merge_into(target: LineAnswer, extra: LineAnswer) -> None:
    """Fill the gaps in a part-specific answer from the rest of the message.

    Only ever fills what is MISSING, and only on an answer that already says
    the part is available -- so a general "3 din" adds an ETA to a part the
    vendor confirmed, and adds nothing to one he refused."""
    if not target.available:
        return
    if target.eta is None and extra.eta is not None:
        target.eta = extra.eta
        target.tat_days = extra.tat_days
    if target.tat_days is None and extra.tat_days is not None:
        target.tat_days = extra.tat_days
    if target.available_qty is None and extra.available_qty is not None:
        target.available_qty = extra.available_qty
    if target.quoted_rate is None and extra.quoted_rate is not None:
        target.quoted_rate = extra.quoted_rate
    if target.mrp is None and extra.mrp is not None:
        target.mrp = extra.mrp
    # An unexplained number anywhere in the message taints the whole reply.
    if extra.ambiguous:
        target.ambiguous = True
