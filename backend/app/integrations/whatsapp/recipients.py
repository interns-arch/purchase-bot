"""Who receives the INTERNAL procurement workbooks on WhatsApp.

The Founder's rule (18 Aug 2026): the allocation and reallocation workbooks
go to the founder/admin number(s) **and** to every registered purchase-team
member (the `register team` list) -- the same people who already receive
every generated PO.

These files are internal by nature: one workbook carries every vendor's
offers for a customer's order, so it must never reach a vendor or a customer.
Only the two internal groups above are ever addressed here."""

from __future__ import annotations

from sqlalchemy import select

from backend.app.integrations.whatsapp import registry
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.models import PurchaseTeamMember
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)


def _team_numbers(session) -> list[str]:
    return [
        number
        for number in session.execute(select(PurchaseTeamMember.whatsapp_number)).scalars()
        if number
    ]


def internal_file_recipients(session=None) -> list[str]:
    """Founder/admin numbers first, then purchase-team members; normalized
    and deduplicated. Pass a `session` to read inside an open transaction,
    or omit it to use a short-lived one.

    Never raises: if the team list cannot be read, the founder still gets the
    file rather than nobody getting it."""
    numbers = [
        registry.normalize_number(number)
        for number in whatsapp_settings.admin_phone_numbers
    ]
    try:
        if session is not None:
            numbers.extend(_team_numbers(session))
        else:
            with get_session() as own_session:
                numbers.extend(_team_numbers(own_session))
    except Exception:  # noqa: BLE001 -- the founder's copy must still go out
        logger.exception(
            "Could not read the purchase-team list -- sending to admin number(s) only."
        )

    deduped: list[str] = []
    for number in numbers:
        if number and number not in deduped:
            deduped.append(number)
    return deduped
