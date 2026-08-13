"""Semantic column inference: understand what a vendor's columns MEAN by
reading the DATA, not just the header text.

Header aliases (`column_detector.INVENTORY_*_HEADERS`) only ever recognise
names we have seen before -- every new vendor spelling ("Net Stock",
"Article No", "Item Code", an unnamed column) is a fresh failure. This module
removes that ceiling: it profiles the actual VALUES in every column and
decides which column is the part number, which is the description, and which
is the on-hand quantity, from how the data behaves:

    part number   alphanumeric codes, nearly all distinct, no sentences
                  ("ABC12345", "1654600Q1FMK", "J9022003")
    description   words and spaces, repeats across rows
                  ("Bearing Assembly", "Dell Laptop")
    quantity      numbers, mostly whole, non-negative, repeats freely
                  (10, 0, 250)

Header text is still used -- as a SCORE HINT and as a hard veto -- never as
the only signal. The money veto is absolute: any column whose header looks
like price / MRP / rate / value / amount / tax / discount can never be
chosen as quantity, exactly as `normalized_validation` enforces downstream.

This layer is deterministic, offline and free: it runs before any AI rescue,
so an unseen header layout usually never needs a model call at all. When it
cannot reach `MIN_CONFIDENCE` it returns None and the existing AI fallback
still gets its turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.ingestion.column_detector import (
    DESCRIPTION_HEADERS,
    INVENTORY_PART_NUMBER_HEADERS,
    INVENTORY_QUANTITY_HEADERS,
    is_parseable_quantity,
    normalise_header,
    parse_quantity,
)

# How much of a column is sampled when profiling (plenty for a stable
# picture, cheap for a 7,000-row file).
_SAMPLE_ROWS = 200

# A column must reach this to be trusted without a model call.
MIN_CONFIDENCE = 0.55

# Header fragments that mean MONEY (or an unconfirmed float) -- these columns
# can never be the available quantity, however numeric they look. Substring
# matching on purpose: "MRP Value", "Unit Rate", "Total Amount", "GST %".
_MONEY_FRAGMENTS = (
    "price", "mrp", "rate", "value", "amount", "cost", "total", "discount",
    "tax", "gst", "vat", "margin", "profit", "revenue", "sale", "purchase",
    "net value", "gross", "float",
)
# Header fragments that suggest an identifier column.
_PART_FRAGMENTS = (
    "part", "product", "item", "material", "sku", "code", "article", "oem",
    "model", "ref", "catalog", "catalogue", "number", "no", "id",
)
# Header fragments that suggest an on-hand count.
_QUANTITY_FRAGMENTS = (
    "qty", "quantity", "stock", "balance", "available", "onhand", "on hand",
    "closing", "net", "free", "count", "pcs", "nos", "units", "inventory",
)
# Header fragments that suggest descriptive text.
_DESCRIPTION_FRAGMENTS = (
    "desc", "name", "particular", "detail", "title", "specification",
)
# Columns that are neither part, description nor quantity -- skipped early.
_DATE_FRAGMENTS = ("date", "time", "month", "year", "period", "expiry")

_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_./#]*$")
_DATE_VALUE_PATTERN = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")


def _contains_any(header: str, fragments: tuple[str, ...]) -> bool:
    text = (header or "").strip().lower()
    return any(fragment in text for fragment in fragments)


def is_money_header(header: str) -> bool:
    """Absolute veto for the quantity role (see module docstring)."""
    return _contains_any(header, _MONEY_FRAGMENTS)


@dataclass
class ColumnProfile:
    """What one column's values actually look like."""

    header: str
    index: int
    # Row-aligned parsed numbers (None where the cell was blank/non-numeric)
    # -- used to compare columns AGAINST EACH OTHER, e.g. spotting a price
    # column that merely sounds like a quantity (see `_tracks_money`).
    numbers: list = field(default_factory=list)
    filled: int = 0
    numeric: int = 0
    whole_numbers: int = 0
    negative: int = 0
    code_like: int = 0          # alphanumeric token, no spaces
    letters_and_digits: int = 0  # "ABC123" -- the strongest part-number tell
    with_spaces: int = 0
    multi_word: int = 0
    date_like: int = 0
    decimals: int = 0
    total_length: int = 0
    distinct: set[str] = field(default_factory=set)

    @property
    def uniqueness(self) -> float:
        return len(self.distinct) / self.filled if self.filled else 0.0

    @property
    def numeric_ratio(self) -> float:
        return self.numeric / self.filled if self.filled else 0.0

    @property
    def code_ratio(self) -> float:
        return self.code_like / self.filled if self.filled else 0.0

    @property
    def alnum_mix_ratio(self) -> float:
        return self.letters_and_digits / self.filled if self.filled else 0.0

    @property
    def word_ratio(self) -> float:
        return self.multi_word / self.filled if self.filled else 0.0

    @property
    def whole_ratio(self) -> float:
        return self.whole_numbers / self.numeric if self.numeric else 0.0

    @property
    def average_length(self) -> float:
        return self.total_length / self.filled if self.filled else 0.0


def profile_column(header: str, index: int, values: list[str]) -> ColumnProfile:
    profile = ColumnProfile(header=header, index=index)
    for raw in values[:_SAMPLE_ROWS]:
        text = (raw or "").strip()
        if not text:
            profile.numbers.append(None)
            continue
        profile.numbers.append(parse_quantity(text) if is_parseable_quantity(text) else None)
        profile.filled += 1
        profile.total_length += len(text)
        profile.distinct.add(text.casefold())

        if _DATE_VALUE_PATTERN.match(text):
            profile.date_like += 1
        if " " in text:
            profile.with_spaces += 1
        if len(text.split()) > 1:
            profile.multi_word += 1
        if _CODE_PATTERN.match(text):
            profile.code_like += 1
        has_alpha = any(character.isalpha() for character in text)
        has_digit = any(character.isdigit() for character in text)
        if has_alpha and has_digit and " " not in text:
            profile.letters_and_digits += 1

        if is_parseable_quantity(text):
            profile.numeric += 1
            number = parse_quantity(text)
            if number == number.to_integral_value():
                profile.whole_numbers += 1
            else:
                profile.decimals += 1
            if number < 0:
                profile.negative += 1
    return profile


def profile_columns(headers: list[str], rows: list[dict[str, str]]) -> list[ColumnProfile]:
    profiles = []
    for index, header in enumerate(headers):
        values = [row.get(header, "") for row in rows[:_SAMPLE_ROWS]]
        profiles.append(profile_column(header, index, values))
    return profiles


def _score_part_number(profile: ColumnProfile) -> float:
    """How much this column behaves like a part/product identifier."""
    if profile.filled < 2:
        return 0.0
    normalised = normalise_header(profile.header)
    score = 0.0

    # Known alias or identifier-ish header text -- a hint, not a decision.
    if normalised in INVENTORY_PART_NUMBER_HEADERS:
        score += 0.45
    elif _contains_any(profile.header, _PART_FRAGMENTS):
        score += 0.25

    # Data shape: codes, not sentences.
    score += 0.30 * profile.alnum_mix_ratio      # "ABC12345" -- strongest tell
    score += 0.20 * profile.code_ratio           # single token, no spaces
    score += 0.25 * profile.uniqueness           # identifiers rarely repeat

    # Anti-signals.
    score -= 0.40 * profile.word_ratio           # descriptions have words
    if profile.date_like > profile.filled * 0.3:
        score -= 0.60
    if is_money_header(profile.header):
        score -= 0.50
    if profile.average_length > 45:              # free text, not a code
        score -= 0.30
    # A purely numeric column can still be a part number, but only when it is
    # highly distinct (a quantity column repeats values constantly).
    if profile.numeric_ratio > 0.95 and profile.uniqueness < 0.75:
        score -= 0.45
    if _contains_any(profile.header, _QUANTITY_FRAGMENTS) and profile.numeric_ratio > 0.8:
        score -= 0.35
    return max(0.0, min(1.0, score))


def _score_quantity(profile: ColumnProfile) -> float:
    """How much this column behaves like an on-hand count."""
    if profile.filled < 2:
        return 0.0
    # Hard vetoes -- money and dates are never quantity.
    if is_money_header(profile.header) or _contains_any(profile.header, _DATE_FRAGMENTS):
        return 0.0
    if profile.numeric_ratio < 0.70:             # a count column is numbers
        return 0.0
    if profile.date_like:
        return 0.0

    normalised = normalise_header(profile.header)
    score = 0.0
    if normalised in INVENTORY_QUANTITY_HEADERS:
        score += 0.50
    elif _contains_any(profile.header, _QUANTITY_FRAGMENTS):
        score += 0.32

    score += 0.32 * profile.numeric_ratio
    score += 0.25 * profile.whole_ratio          # stock counts are whole
    # Counts look like counts: short, whole, plausible magnitudes. (Kept
    # modest so it can never outweigh the money veto above.)
    if profile.decimals == 0 and profile.average_length <= 6:
        score += 0.10
    # Counts repeat (many rows share 0/1/2); prices are nearly all distinct.
    # Weak on purpose -- a small file's column is distinct by accident.
    score += 0.10 * (1.0 - profile.uniqueness)

    if profile.negative:
        score -= 0.15                            # rare but legal (returns)
    if profile.decimals > profile.numeric * 0.5:
        score -= 0.25                            # money-ish granularity
    if profile.average_length > 12:              # long digit strings = codes
        score -= 0.25
    return max(0.0, min(1.0, score))


def _score_description(profile: ColumnProfile) -> float:
    """How much this column behaves like a product name/description."""
    if profile.filled < 2:
        return 0.0
    normalised = normalise_header(profile.header)
    score = 0.0
    if normalised in DESCRIPTION_HEADERS:
        score += 0.45
    elif _contains_any(profile.header, _DESCRIPTION_FRAGMENTS):
        score += 0.30

    score += 0.40 * profile.word_ratio           # real names have words
    score += 0.15 * (profile.with_spaces / profile.filled if profile.filled else 0)
    if profile.numeric_ratio > 0.8:
        score -= 0.60                            # a number column is not a name
    if is_money_header(profile.header):
        score -= 0.40
    if profile.average_length < 4:
        score -= 0.25
    return max(0.0, min(1.0, score))


def _tracks_money(candidate: ColumnProfile, money_columns: list[ColumnProfile]) -> bool:
    """True when this column moves in near-fixed proportion to a known money
    column -- i.e. it IS money, whatever its header claims.

    Real vendor files exist where the headers lie: a "Balance" column holding
    4500 / 390 / 12 beside an "MRP" of 5200 / 450 / 15 is a net-price column
    (a steady ~0.86 of MRP on every row), not a stock count. Genuine
    quantities have no such fixed relationship to price -- 150 fuses at Rs.15
    and 18 mounts at Rs.5200 produce wildly different ratios. Comparing
    columns to each other is the only way to see this; no header text can."""
    for money in money_columns:
        ratios = []
        for left, right in zip(candidate.numbers, money.numbers):
            if left is None or right is None or right == 0:
                continue
            ratios.append(abs(float(left) / float(right)))
        if len(ratios) < 3:
            continue
        mean = sum(ratios) / len(ratios)
        if mean <= 0:
            continue
        spread = (sum((r - mean) ** 2 for r in ratios) / len(ratios)) ** 0.5 / mean
        if spread < 0.25:  # steady proportion of a price => this is money
            return True
    return False


@dataclass
class InferredColumns:
    part_number: str
    quantity: str
    description: str | None
    confidence: float
    explanation: str


def infer_inventory_columns(
    headers: list[str], rows: list[dict[str, str]]
) -> InferredColumns | None:
    """Decide which columns hold the part number, quantity and description by
    reading the data. Returns None when the evidence is too weak (the caller
    then falls back to the AI rescue), so a wrong guess is never preferred
    over asking for help."""
    usable_headers = [header for header in headers if str(header).strip()]
    if len(usable_headers) < 2 or not rows:
        return None

    profiles = [p for p in profile_columns(usable_headers, rows) if p.filled]
    if len(profiles) < 2:
        return None

    part_scores = {p.header: _score_part_number(p) for p in profiles}
    quantity_scores = {p.header: _score_quantity(p) for p in profiles}
    description_scores = {p.header: _score_description(p) for p in profiles}

    part_header = max(part_scores, key=lambda h: part_scores[h])
    part_score = part_scores[part_header]

    # The quantity column must be a DIFFERENT column from the part number,
    # and must not merely track a price column (headers can lie -- see
    # `_tracks_money`). Dropping a disguised price here is what makes the
    # inference safe: with no honest candidate left we return None and the AI
    # rescue decides, instead of importing a price as stock.
    money_columns = [p for p in profiles if is_money_header(p.header) and p.numeric]
    quantity_candidates = {
        header: score
        for header, score in quantity_scores.items()
        if header != part_header and score > 0
    }
    for profile in profiles:
        if profile.header in quantity_candidates and _tracks_money(profile, money_columns):
            del quantity_candidates[profile.header]
    if not quantity_candidates:
        return None
    quantity_header = max(quantity_candidates, key=lambda h: quantity_candidates[h])
    quantity_score = quantity_candidates[quantity_header]

    if part_score < MIN_CONFIDENCE or quantity_score < MIN_CONFIDENCE:
        return None

    description_candidates = {
        h: s for h, s in description_scores.items() if h not in (part_header, quantity_header)
    }
    description_header = None
    if description_candidates:
        best = max(description_candidates, key=lambda h: description_candidates[h])
        if description_candidates[best] >= 0.45:
            description_header = best

    confidence = round((part_score + quantity_score) / 2, 2)
    explanation = (
        f"Columns identified from the data itself (no fixed header names): "
        f"part_number={part_number_note(part_header, part_score)}, "
        f"quantity={part_number_note(quantity_header, quantity_score)}"
        + (f", description={description_header!r}" if description_header else "")
        + "."
    )
    return InferredColumns(
        part_number=part_header,
        quantity=quantity_header,
        description=description_header,
        confidence=confidence,
        explanation=explanation,
    )


def part_number_note(header: str, score: float) -> str:
    return f"{header!r} (confidence {score:.2f})"


def detect_header_row_by_data(
    grid: list[list[str]], *, max_scan_rows: int = 60, max_data_rows: int = 60
) -> int | None:
    """Find the line-item header row in a file whose header NAMES are all
    unrecognised, by testing candidate rows and keeping the one whose data
    below it profiles best. Used only after the alias-based
    `column_detector.detect_header_row` finds nothing."""
    best_index: int | None = None
    best_confidence = 0.0

    for index, row in enumerate(grid[:max_scan_rows]):
        headers = [str(cell).strip() for cell in row]
        filled = [h for h in headers if h]
        if len(filled) < 2:
            continue
        # Duplicate header names make a dict-of-row unusable -- skip.
        if len(set(h.casefold() for h in filled)) != len(filled):
            continue
        # A HEADER row is labels; a DATA row contains values. Without this
        # check the scan happily treats the first data row as the header
        # (columns then get named "P-1016" / "18") and silently shifts every
        # row by one. Any bare number in the row means this is data.
        if any(is_parseable_quantity(cell) for cell in filled):
            continue

        data_rows: list[dict[str, str]] = []
        for data_row in grid[index + 1 : index + 1 + max_data_rows]:
            cells = [str(value).strip() for value in data_row]
            if not any(cells):
                break
            padded = cells + [""] * max(0, len(headers) - len(cells))
            data_rows.append({headers[i]: padded[i] for i in range(len(headers)) if headers[i]})
        if len(data_rows) < 2:
            continue

        inferred = infer_inventory_columns([h for h in headers if h], data_rows)
        if inferred is not None and inferred.confidence > best_confidence:
            best_confidence = inferred.confidence
            best_index = index

    return best_index
