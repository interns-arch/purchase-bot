"""The monthly MRP list (Founder, 2 Nov 2026). On the 1st of every month the bot
asks in every vendor WhatsApp group for the MRP list; what the vendor sends back
is saved per part and used for the Dealer Portal price.

WHY IT IS NOT JUST ANOTHER STOCK FILE. The Portal's only upload is the stock CSV
(`part_no,quantity,price,...`), which REPLACES the quantity of every part in it,
and an MRP list has no quantities. So the list is never imported as stock and
never uploaded by itself. It is saved here, and `dealer_portal.delta` uses it as
the price whenever it builds a push:

    the stock file's own Rate  ->  this MRP list  ->  the stock file's MRP  ->  older prices

Quantities always come from the vendor's stock file. A part on the list that the
vendor has no stock of stays saved and reaches the Portal when stock arrives.

WHICH FILE IS AN MRP LIST. A stock file with an MRP column is our normal stock
format and must stay stock, so a file counts only when:
  - its caption or name says MRP / price list / rate list (and not "stock"), or
  - it has MRP but no quantity column, sent in the days after the request by a
    vendor who has not sent their list this month.

MRP_LIST_ENABLED=false (the default) leaves everything exactly as it was: no
request, every file goes to stock, and Portal prices are not touched. The request
also needs the group side on (WHAPI_ENABLED)."""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from backend.app.integrations.whatsapp.models import VendorMrpItem, VendorMrpList
from core.ingestion.column_detector import (
    DESCRIPTION_HEADERS,
    INVENTORY_PART_NUMBER_HEADERS,
    INVENTORY_QUANTITY_HEADERS,
    MRP_HEADERS,
    PRICE_HEADERS,
    find_optional_column,
    is_parseable_quantity,
    normalise_part_number,
    parse_quantity,
)
from core.logging_setup import get_logger
from core.time_utils import now_ist, now_ist_naive

logger = get_logger(__name__)

REQUEST_TEXT = "Namaste ji 🙏 is mahine ki MRP list share kar dijiye (Excel mein: Part No, Part Description, MRP)."
SEND_AS_EXCEL = "Ji, MRP list Excel mein bhej dijiye: Part No, Part Description, MRP."
NOT_READABLE = "Ji, is file mein Part No aur MRP ke column nahi mile. Excel mein bhej dijiye: Part No, Part Description, MRP."

SPREADSHEETS = {".xlsx", ".xlsm", ".xls", ".csv"}
_HEADER_SEARCH_ROWS = 30
_NAMED = re.compile(r"(?<![a-z0-9])(mrp|price[\s_-]*list|rate[\s_-]*list)(?![a-z0-9])", re.IGNORECASE)
_STOCK = re.compile(r"(?<![a-z0-9])stock(?![a-z0-9])", re.IGNORECASE)


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    return _env("MRP_LIST_ENABLED", "false").lower() == "true"


def asking_enabled() -> bool:
    from backend.app.integrations.whatsapp import whapi

    return enabled() and whapi.enabled()


def request_day() -> int:
    return int(_env("MRP_REQUEST_DAY", "1"))


def request_time() -> str:
    return _env("MRP_REQUEST_TIME", "10:00")


def month_key() -> str:
    return now_ist().strftime("%Y-%m")


# ------------------------------------------------------------------ the request
def received_vendor_ids(month: str, session) -> set[int]:
    rows = session.execute(select(VendorMrpList.vendor_id).where(VendorMrpList.month == month).distinct())
    return set(rows.scalars().all())


def _received_codes(month: str) -> set[str]:
    from core.db import get_session
    from core.models import Vendor

    with get_session() as session:
        rows = session.execute(
            select(Vendor.vendor_code).where(Vendor.id.in_(received_vendor_ids(month, session)))
        ).scalars().all()
    return {code.upper() for code in rows if code}


def send_requests(*, pause=(20, 45), sleep=time.sleep) -> dict:
    """Post the request in every vendor group, one by one with a pause so 40+
    groups never go out as one burst. A vendor who already sent this month's
    list is not asked again. Returns what happened."""
    from backend.app.integrations.whatsapp import whapi
    from backend.app.notifications import broker

    if not asking_enabled():
        return {"sent": [], "skipped": [], "failed": []}
    done = _received_codes(month_key())
    sent, skipped, failed = [], [], []
    member_of = {g.get("id") for g in whapi.list_groups()}
    for group, code in whapi.vendor_group_codes().items():
        if (member_of and group not in member_of) or code.upper() in done:
            skipped.append(group)
            continue
        if sent or failed:
            sleep(random.uniform(*pause))
        try:
            whapi.post_as_owner(group, REQUEST_TEXT)
            sent.append(group)
        except Exception:  # noqa: BLE001 -- one group must not stop the rest
            logger.exception("MRP request: could not post in %s (%s).", group, code)
            failed.append(group)
    summary = f"Monthly MRP request: asked in {len(sent)} vendor groups, {len(skipped)} skipped, {len(failed)} failed"
    logger.info(summary)
    whapi.record("", "mrp request", None, summary)
    if failed or not sent:
        # It runs once a month: nobody would otherwise notice it did not go out.
        broker.publish("warning", "The monthly MRP request did not reach every vendor group.", summary)
    return {"sent": sent, "skipped": skipped, "failed": failed}


def send_requests_safe() -> None:
    try:
        send_requests()
    except Exception:  # noqa: BLE001 -- a scheduler job must never raise
        logger.exception("Monthly MRP request failed.")


# ------------------------------------------------------------------ which file
def is_named(caption: str | None, filename: str | None) -> bool:
    """The vendor says it is an MRP list. "Stock with MRP.xlsx" is still stock."""
    text = f"{caption or ''} {filename or ''}"
    return bool(_NAMED.search(text)) and not _STOCK.search(text)


def worth_opening(vendor_id: int, session) -> bool:
    """A file with no MRP name is opened only in the days after the request, and
    only until this vendor's list has come, so stock files are not read twice
    all month."""
    today = now_ist()
    first = today.replace(day=min(request_day(), 28))
    if not first.date() <= today.date() < (first + timedelta(days=int(_env("MRP_LIST_WINDOW_DAYS", "7")))).date():
        return False
    return vendor_id not in received_vendor_ids(month_key(), session)


# ------------------------------------------------------------------ reading it
@dataclass
class ParsedList:
    items: dict[str, tuple[str, str | None, Decimal]] = field(default_factory=dict)  # part -> (as written, name, mrp)
    bad_rows: int = 0
    has_mrp_column: bool = False
    has_quantity_column: bool = False


def read_list(path: Path) -> ParsedList | None:
    """The part/MRP table anywhere in the file's first rows, or None when there
    is no such table. A vendor's own "Rate" counts only when there is no MRP."""
    from core.ingestion.csv_reader import read_csv_grid
    from core.ingestion.excel_reader import read_excel_grid

    grid = read_csv_grid(path) if path.suffix.lower() == ".csv" else read_excel_grid(path)
    for index, row in enumerate(grid[:_HEADER_SEARCH_ROWS]):
        headers = [str(cell).strip() for cell in row]
        part_column = find_optional_column(headers, INVENTORY_PART_NUMBER_HEADERS)
        mrp_column = find_optional_column(headers, MRP_HEADERS)
        price_column = mrp_column or find_optional_column(headers, PRICE_HEADERS)
        if part_column and price_column:
            break
    else:
        return None

    parsed = ParsedList(
        has_mrp_column=bool(mrp_column),
        has_quantity_column=bool(find_optional_column(headers, INVENTORY_QUANTITY_HEADERS)),
    )
    name_column = find_optional_column(headers, DESCRIPTION_HEADERS)
    for data in grid[index + 1:]:
        cells = dict(zip(headers, (str(cell).strip() for cell in data)))
        if not any(cells.values()):
            continue
        raw_part = cells.get(part_column, "")
        part = normalise_part_number(raw_part)
        raw_mrp = cells.get(price_column, "")
        mrp = parse_quantity(raw_mrp) if is_parseable_quantity(raw_mrp) else None
        if not part or mrp is None or mrp <= 0:
            parsed.bad_rows += 1
            continue
        # The same part twice: the higher figure, as the Portal push does.
        if part not in parsed.items or mrp > parsed.items[part][2]:
            parsed.items[part] = (raw_part, (cells.get(name_column) or None) if name_column else None, mrp)
    return parsed


def is_list(parsed: ParsedList | None, *, named: bool) -> bool:
    """A named file needs only a part and a price. An unnamed one must have MRP
    and no quantity, or it is an ordinary stock file."""
    if parsed is None or not parsed.items:
        return False
    return named or (parsed.has_mrp_column and not parsed.has_quantity_column)


# ------------------------------------------------------------------ saving it
def save(vendor_id: int, parsed: ParsedList, file_name: str | None, session) -> VendorMrpList:
    """Newest MRP per part wins. A part left out of this month's list keeps the
    MRP it had: a shorter list does not mean the part stopped having one."""
    month, now = month_key(), now_ist_naive()
    existing = {
        item.normalized_part_number: item
        for item in session.execute(select(VendorMrpItem).where(VendorMrpItem.vendor_id == vendor_id)).scalars()
    }
    for part, (raw_part, name, mrp) in parsed.items.items():
        item = existing.get(part)
        if item is None:
            item = VendorMrpItem(vendor_id=vendor_id, normalized_part_number=part)
            session.add(item)
        item.part_no, item.description, item.mrp, item.list_month, item.updated_at = raw_part, name, mrp, month, now
    received = VendorMrpList(
        vendor_id=vendor_id, month=month, file_name=file_name,
        parts=len(parsed.items), bad_rows=parsed.bad_rows, created_at=now,
    )
    session.add(received)
    session.flush()
    return received


def reply_for(parsed: ParsedList) -> str:
    text = f"✅ MRP list mil gayi ji, {len(parsed.items)} parts save ho gaye."
    if parsed.bad_rows:
        text += f" {parsed.bad_rows} lines mein Part No ya MRP sahi nahi tha."
    return text


# ------------------------------------------------------------------ the Portal
def prices_for(vendor_ids: list[int], parts: list[str], session) -> dict[str, Decimal]:
    """The saved MRP for these parts, for one Portal account's vendors. Empty when
    the feature is off, so the Portal push behaves exactly as before. Two vendors
    of one account with the same part: the higher, as the push does."""
    if not enabled() or not vendor_ids or not parts:
        return {}
    prices: dict[str, Decimal] = {}
    rows = session.execute(
        select(VendorMrpItem.normalized_part_number, VendorMrpItem.mrp)
        .where(VendorMrpItem.vendor_id.in_(vendor_ids), VendorMrpItem.normalized_part_number.in_(parts))
    )
    for part, mrp in rows:
        prices[part] = max(prices.get(part, mrp), mrp)
    return prices
