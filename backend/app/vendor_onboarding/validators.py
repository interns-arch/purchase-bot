"""Field validators for the vendor form. Each returns (clean value, None) or
(None, message for the vendor). Pure functions -- no I/O."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# GST state codes 01-38 plus 97 (other territory) and 99 (centre jurisdiction).
_STATE_CODES = {f"{n:02d}" for n in range(1, 39)} | {"97", "99"}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def gstin_checksum_ok(gstin: str) -> bool:
    """The official GSTIN check digit (15th character)."""
    total = 0
    for index, char in enumerate(gstin[:14]):
        value = _GST_CHARS.index(char) * (2 if index % 2 else 1)
        total += value // 36 + value % 36
    return _GST_CHARS[(36 - total % 36) % 36] == gstin[14]


def gstin(value: str) -> tuple[str | None, str | None]:
    code = _compact(value)
    if not _GSTIN_RE.match(code):
        return None, "That GSTIN doesn't look right — it should be 15 characters, e.g. 07AALFN8339A1Z8."
    if code[:2] not in _STATE_CODES:
        return None, f"'{code[:2]}' is not a valid GST state code. Please check the GSTIN."
    if not gstin_checksum_ok(code):
        return None, "That GSTIN fails the GST check digit — please check for a typing mistake."
    return code, None


def pan(value: str, *, gstin_value: str | None = None) -> tuple[str | None, str | None]:
    code = _compact(value)
    if not _PAN_RE.match(code):
        return None, "That PAN doesn't look right — it should be like AALFN8339A (5 letters, 4 digits, 1 letter)."
    if gstin_value and gstin_value[2:12] != code:
        return None, (
            f"This PAN doesn't match your GSTIN (the GSTIN contains PAN {gstin_value[2:12]}). "
            "Please check."
        )
    return code, None


def ifsc(value: str) -> tuple[str | None, str | None]:
    code = _compact(value)
    if not _IFSC_RE.match(code):
        return None, "That IFSC doesn't look right — it should be 11 characters like SBIN0001234."
    return code, None


def mobile(value: str) -> tuple[str | None, str | None]:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None, "Please send a valid 10-digit mobile number."
    return digits, None


def email(value: str) -> tuple[str | None, str | None]:
    text = (value or "").strip()
    if not _EMAIL_RE.match(text):
        return None, "That email doesn't look right — please send it like name@company.com."
    return text.lower(), None


def account_number(value: str) -> tuple[str | None, str | None]:
    digits = re.sub(r"[\s-]", "", value or "")
    if not digits.isdigit() or not 9 <= len(digits) <= 18:
        return None, "A bank account number has 9 to 18 digits — please check and send again."
    return digits, None


def _number(value: str) -> Decimal | None:
    text = re.sub(r"[₹,\s]|rs\.?|inr|/-", "", (value or "").lower())
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def days(value: str) -> tuple[str | None, str | None]:
    number = _number(re.sub(r"days?", "", (value or "").lower()))
    if number is None or number < 0 or number > 365 or number != number.to_integral_value():
        return None, "Please send the number of days, e.g. 30."
    return str(int(number)), None


def money(value: str) -> tuple[str | None, str | None]:
    text = (value or "").lower().replace("lakh", "00000").replace("lac", "00000")
    number = _number(text)
    if number is None or number < 0:
        return None, "Please send an amount in rupees, e.g. 500000."
    return str(number.quantize(Decimal("1")) if number == number.to_integral_value() else number), None


def percent(value: str) -> tuple[Decimal | None, str | None]:
    number = _number((value or "").replace("%", ""))
    if number is None or not 0 <= number <= 100:
        return None, "The discount must be a percentage between 0 and 100."
    return number, None


def text(value: str, *, min_length: int = 2) -> tuple[str | None, str | None]:
    cleaned = " ".join((value or "").split())
    if len(cleaned) < min_length:
        return None, "Please send a proper answer."
    return cleaned, None


_BRAND_LINE = re.compile(r"^(?P<brand>.+?)\s*[-–:=]?\s*(?P<pct>\d+(?:\.\d+)?)\s*%?$")


def brand_discounts(value: str) -> tuple[list[dict] | None, str | None]:
    """'Maruti - 12\\nHyundai 10%' -> [{"brand": "MARUTI", "discount_pct": "12"}, ...]."""
    rows: list[dict] = []
    for raw_line in re.split(r"[\n;,]+", value or ""):
        line = raw_line.strip()
        if not line:
            continue
        match = _BRAND_LINE.match(line)
        if not match:
            return None, (
                f"I couldn't read '{line}'. Send one brand per line with its discount, e.g.\n"
                "Maruti - 12\nHyundai - 10"
            )
        pct, error = percent(match.group("pct"))
        if error:
            return None, f"{line}: {error}"
        rows.append({"brand": match.group("brand").strip(" -–:").upper(), "discount_pct": str(pct)})
    if not rows:
        return None, "Send at least one brand with its discount, e.g. Maruti - 12"
    return rows, None
