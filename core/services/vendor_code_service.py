"""Vendor Code identification: the permanent identifier used throughout the
app for inventory imports (manual and WhatsApp alike), replacing WhatsApp
sender-number identification -- every vendor messages the same shared
WhatsApp Business number, so the sender's phone number can never tell them
apart. A vendor's own inventory filename carries the code instead
(`AR_CT_Inventory.xlsx`).

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Vendor

_CODE_SUFFIX = "_CT"
_CODE_PREFIX_PATTERN = re.compile(r"^([A-Z]{2}_CT(?:_\d+)?)_")


def generate_vendor_code(name: str, session: Session) -> str:
    """First two alphabetic characters of `name`, uppercased, + "_CT"; on
    collision with an existing vendor's code, appends _2, _3, ... until
    unique. Falls back to "XX" if `name` has fewer than two letters."""
    letters = "".join(ch for ch in name if ch.isalpha())[:2].upper()
    letters = letters.ljust(2, "X")
    base_code = f"{letters}{_CODE_SUFFIX}"

    if get_vendor_by_code(base_code, session) is None:
        return base_code

    suffix = 2
    while True:
        candidate = f"{base_code}_{suffix}"
        if get_vendor_by_code(candidate, session) is None:
            return candidate
        suffix += 1


def parse_vendor_code_from_filename(filename: str) -> str | None:
    """Returns the leading vendor-code prefix (e.g. "AR_CT" or "AR_CT_2")
    from a filename like "AR_CT_Inventory.xlsx", or `None` if the filename
    has no code-shaped prefix at all. Case-insensitive."""
    match = _CODE_PREFIX_PATTERN.match(filename.strip().upper())
    return match.group(1) if match else None


def get_vendor_by_code(code: str, session: Session) -> Vendor | None:
    return session.execute(
        select(Vendor).where(func.upper(Vendor.vendor_code) == code.upper())
    ).scalar_one_or_none()
