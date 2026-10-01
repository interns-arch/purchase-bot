"""A vendor's stock list sent as a PHOTO (or a scanned PDF).

    vendor:  [photo of his stock page]
    bot:     📷 Photo mil gayi, padh rahe hain…
    bot:     Photo se ye 12 part padhe:
             1. 16510M68K10 — 5
             ...
             Sahi hai? *haan* likhiye to stock update ho jayega, *nahi* to cancel.
    vendor:  haan
    bot:     ✅ Stock updated from your message. ...

The photo is read by `backend.app.ai.vision`; the lines go through the same
typed-list reader as a WhatsApp message; nothing is imported until he says
haan. The import then works exactly like a typed list (merge mode: only the
parts on the photo change -- a photo of one page must not wipe the rest).
Founder's decision, 1 Oct 2026: confirm with the vendor before import.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import WhatsAppPendingPhotoStock
from backend.app.integrations.whatsapp.vendor_text_stock import ParsedStockText, StockLine, parse_stock_text
from core.ingestion.column_detector import decimal_to_string
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

HOLD_HOURS = 24
RECEIVED = "📷 Photo mil gayi, padh rahe hain… (1 minute)"
CANNOT_READ = (
    "❌ Photo se stock nahi padh paaye ({why}).\n"
    "Please Excel bhejiye, ya stock type kar dijiye — har line mein part number aur quantity, "
    "jaise: 16510M68K10 5"
)
CANCELLED = "❌ Theek hai, photo wala stock cancel kar diya. Excel bhejiye ya type kar dijiye."

_YES = re.compile(r"^\s*(haan+|han|haa|ha|hn|yes|y|ok|okay|sahi|sahi hai|theek|thik|theek hai|confirm|done|1)\s*[.!]*\s*$", re.I)
_NO = re.compile(r"^\s*(nahi+|nhi|no|n|galat|galat hai|cancel|mat karo|2)\s*[.!]*\s*$", re.I)
_LINES_PER_MESSAGE = 40


def answer(text: str | None) -> str | None:
    """"yes", "no", or None when the text is not an answer."""
    if not text:
        return None
    if _YES.match(text):
        return "yes"
    if _NO.match(text):
        return "no"
    return None


def read_lines(model_text: str) -> ParsedStockText:
    """The model's transcription, read like a typed list. A line the model
    marked "?" is reported, never imported."""
    clean_lines, unsure = [], []
    for line in model_text.splitlines():
        if not line.strip():
            continue
        (unsure if "?" in line else clean_lines).append(line.strip().strip("*`").strip())
    parsed = parse_stock_text("\n".join(clean_lines))
    parsed.unreadable = unsure + parsed.unreadable
    return parsed


def hold(number: str, vendor_id: int, parsed: ParsedStockText, session: Session, *, source_filename: str | None, model: str | None) -> None:
    for old in session.execute(select(WhatsAppPendingPhotoStock).where(WhatsAppPendingPhotoStock.whatsapp_number == number)).scalars():
        session.delete(old)
    session.add(
        WhatsAppPendingPhotoStock(
            whatsapp_number=number,
            vendor_id=vendor_id,
            lines=[{"part": l.part_number, "qty": decimal_to_string(l.quantity), "description": l.description} for l in parsed.lines],
            unreadable=list(parsed.unreadable),
            source_filename=source_filename,
            model=model,
        )
    )
    session.flush()


def has_pending(number: str, session: Session) -> bool:
    return session.execute(
        select(WhatsAppPendingPhotoStock.id).where(WhatsAppPendingPhotoStock.whatsapp_number == number).limit(1)
    ).first() is not None


def take(number: str, session: Session) -> tuple[int, ParsedStockText] | None:
    """The held lines for this number (vendor_id, parsed), removed from the
    hold. None when nothing is held or it has expired."""
    row = session.execute(
        select(WhatsAppPendingPhotoStock)
        .where(WhatsAppPendingPhotoStock.whatsapp_number == number)
        .order_by(WhatsAppPendingPhotoStock.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    vendor_id = row.vendor_id
    fresh = row.created_at is None or now_ist_naive() - row.created_at <= timedelta(hours=HOLD_HOURS)
    parsed = ParsedStockText(
        lines=[StockLine(part_number=l["part"], quantity=Decimal(l["qty"]), description=l.get("description") or "") for l in row.lines or []],
        unreadable=list(row.unreadable or []),
    )
    for old in session.execute(select(WhatsAppPendingPhotoStock).where(WhatsAppPendingPhotoStock.whatsapp_number == number)).scalars():
        session.delete(old)
    session.flush()
    return (vendor_id, parsed) if fresh else None


def confirmation_messages(parsed: ParsedStockText) -> list[str]:
    """Every line read, numbered, split into WhatsApp-sized messages, the
    question last. He must see ALL of it to confirm it."""
    rows = [f"{i}. {l.part_number} — {decimal_to_string(l.quantity)}" for i, l in enumerate(parsed.lines, start=1)]
    chunks = [rows[i : i + _LINES_PER_MESSAGE] for i in range(0, len(rows), _LINES_PER_MESSAGE)] or [[]]
    messages = []
    for index, chunk in enumerate(chunks):
        head = f"Photo se ye {len(parsed.lines)} part padhe:\n" if index == 0 else ""
        messages.append(head + "\n".join(chunk))
    tail = []
    if parsed.unreadable:
        tail.append(f"⚠️ Ye {len(parsed.unreadable)} line saaf nahi padhi gayi (import nahi hongi): " + "; ".join(parsed.unreadable[:5]))
    tail.append("Sahi hai? *haan* likhiye to stock update ho jayega, *nahi* likhiye to cancel.")
    messages[-1] = messages[-1] + "\n\n" + "\n".join(tail)
    return messages
