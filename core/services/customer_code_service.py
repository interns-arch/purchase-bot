"""Customer Code identification: mirrors `core.services.vendor_code_service`
for Customer Orders arriving over WhatsApp, where multiple customers may
send files one after another from the founder's side -- there is no
per-customer WhatsApp number, so a Customer Order's filename carries the
code instead (`AB_CO_Order.xlsx`), exactly like a vendor's inventory file.

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Customer

_CODE_SUFFIX = "_CO"
_CODE_PREFIX_PATTERN = re.compile(r"^([A-Z]{2}_CO(?:_\d+)?)_")


def generate_customer_code(name: str, session: Session) -> str:
    """First two alphabetic characters of `name`, uppercased, + "_CO"; on
    collision with an existing customer's code, appends _2, _3, ... until
    unique. Falls back to "XX" if `name` has fewer than two letters."""
    letters = "".join(ch for ch in name if ch.isalpha())[:2].upper()
    letters = letters.ljust(2, "X")
    base_code = f"{letters}{_CODE_SUFFIX}"

    if get_customer_by_code(base_code, session) is None:
        return base_code

    suffix = 2
    while True:
        candidate = f"{base_code}_{suffix}"
        if get_customer_by_code(candidate, session) is None:
            return candidate
        suffix += 1


def parse_customer_code_from_filename(filename: str) -> str | None:
    """Returns the leading customer-code prefix (e.g. "AB_CO" or "AB_CO_2")
    from a filename like "AB_CO_Order.xlsx", or `None` if the filename has no
    code-shaped prefix at all. Case-insensitive."""
    match = _CODE_PREFIX_PATTERN.match(filename.strip().upper())
    return match.group(1) if match else None


def get_customer_by_code(code: str, session: Session) -> Customer | None:
    return session.execute(
        select(Customer).where(func.upper(Customer.customer_code) == code.upper())
    ).scalar_one_or_none()
