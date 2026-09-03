"""The OPTIONAL single reminder for an unconfirmed sales quote.

Founder asked whether the bot should follow up after some minutes. It can --
but it is OFF by default and deliberately so: a stock check is a question,
most questions are idle, and ten people asking all day would turn periodic
"shall I confirm?" messages into noise the team learns to ignore.

When `WHATSAPP_SALES_NUDGE_MINUTES` is set, each quote gets exactly ONE
reminder, never repeated (`SalesQuote.nudged_at` records it). Quotes past
their time-to-live are expired instead of nudged -- reminding someone about
an answer that is no longer safe to act on would be worse than silence.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.models import SalesQuote
from backend.app.integrations.whatsapp.outbound import send_reply_safe
from core.db import get_session
from core.logging_setup import get_logger
from core.time_utils import utcnow_naive

logger = get_logger(__name__)


def send_pending_nudges() -> int:
    """One reminder per still-unconfirmed quote. Returns how many were sent.
    Never raises -- it is a scheduled background sweep."""
    minutes = whatsapp_settings.sales_nudge_minutes
    if minutes <= 0:
        return 0

    ttl = whatsapp_settings.sales_quote_ttl_minutes
    now = utcnow_naive()
    due: list[tuple[str, str, int]] = []
    try:
        with get_session() as session:
            for quote in session.execute(
                select(SalesQuote).where(SalesQuote.status == "PENDING")
            ).scalars():
                if quote.created_at is None:
                    continue
                age = now - quote.created_at
                if ttl > 0 and age > timedelta(minutes=ttl):
                    quote.status = "EXPIRED"  # too old to act on -- do not nudge
                    continue
                if quote.nudged_at is not None or age < timedelta(minutes=minutes):
                    continue
                quote.nudged_at = now
                due.append((quote.whatsapp_number, quote.reference, len(quote.parts or [])))
    except Exception:  # noqa: BLE001
        logger.exception("Sales nudge sweep could not read pending quotes.")
        return 0

    sent = 0
    for number, reference, parts in due:
        try:
            if send_reply_safe(
                number,
                f"[{reference}] {parts} part(s) still not ordered. "
                'Reply "confirm <customer name>" to place it, or "cancel".',
            ):
                sent += 1
        except Exception:  # noqa: BLE001 -- one failure must not stop the rest
            logger.exception("Could not nudge %s about quote %s.", number, reference)
    if sent:
        logger.info("Sales nudge: reminded %d unconfirmed quote(s).", sent)
    return sent
