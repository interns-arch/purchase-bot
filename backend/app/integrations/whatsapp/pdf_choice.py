"""A vendor's PDF held while the bot asks "stock list hai ya bill?".

`classify_vendor_pdf` decides from the PDF's own words (see
`core.ingestion.pdf_reader.classify_pdf`). Only when that is unclear is the
file held here and the vendor asked; his next reply of "stock" or "bill"
releases it. Anything else he types is handled as usual -- the question
never swallows an unrelated message. A held file older than
`HOLD_HOURS` is dropped quietly: by then a fresh file is the better answer.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import WhatsAppPendingPdfChoice
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

HOLD_HOURS = 24

QUESTION = (
    "📄 Ye PDF *stock list* hai ya *bill*?\n"
    "Reply karein: *stock* ya *bill*"
)

_STOCK_ANSWER = re.compile(r"^\s*(stock|stok|stock\s*list|inventory|maal|1)\s*[.!]*\s*$", re.I)
_BILL_ANSWER = re.compile(r"^\s*(bill|invoice|bil|tax\s*invoice|2)\s*[.!]*\s*$", re.I)


def classify_vendor_pdf(file_path: Path) -> tuple[str, str]:
    """("stock" | "invoice" | "unclear" | "scanned", why). Never raises: a PDF
    that cannot even be opened is "unclear" and the vendor is asked."""
    try:
        from core.ingestion.pdf_reader import classify_pdf, read_pdf

        return classify_pdf(read_pdf(file_path))
    except Exception as exc:  # noqa: BLE001 -- a broken PDF is a question, not a crash
        logger.warning("Could not read PDF %s to classify it: %s", file_path, exc)
        return "unclear", f"the PDF could not be opened ({exc})"


def hold(
    number: str,
    vendor_id: int,
    file_path: Path,
    original_filename: str,
    session: Session,
    *,
    media_id: str | None = None,
    message_id: str | None = None,
) -> None:
    session.add(
        WhatsAppPendingPdfChoice(
            whatsapp_number=number,
            vendor_id=vendor_id,
            staged_path=str(file_path),
            original_filename=original_filename,
            media_id=media_id,
            message_id=message_id,
            created_at=now_ist_naive(),
        )
    )
    session.flush()


def answer(text: str | None) -> str | None:
    """"stock", "invoice", or None when the text is not an answer at all."""
    if not text:
        return None
    if _STOCK_ANSWER.match(text):
        return "stock"
    if _BILL_ANSWER.match(text):
        return "invoice"
    return None


def take(number: str, session: Session, now: datetime | None = None) -> list[WhatsAppPendingPdfChoice]:
    """This number's held PDFs, oldest first, removed from the hold. Expired
    ones are dropped and not returned."""
    now = now or now_ist_naive()
    rows = list(
        session.execute(
            select(WhatsAppPendingPdfChoice)
            .where(WhatsAppPendingPdfChoice.whatsapp_number == number)
            .order_by(WhatsAppPendingPdfChoice.id)
        ).scalars()
    )
    fresh: list[WhatsAppPendingPdfChoice] = []
    for row in rows:
        created = row.created_at or now
        if now - created <= timedelta(hours=HOLD_HOURS):
            fresh.append(row)
        session.delete(row)
    session.flush()
    for row in fresh:
        session.expunge(row)
    return fresh


def has_pending(number: str, session: Session) -> bool:
    return (
        session.execute(
            select(WhatsAppPendingPdfChoice.id).where(WhatsAppPendingPdfChoice.whatsapp_number == number).limit(1)
        ).first()
        is not None
    )
