"""Per-WhatsApp-number pending-command persistence (backed by
`WhatsAppPendingCommand`). One row per number, so concurrent users route
independently; persisted in the DB rather than in memory so a restart (or a
future multi-process deployment) never loses a user's pending command.

Stores/returns the canonical command KEY (see
`backend.app.integrations.whatsapp.commands`), never raw user text."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import WhatsAppPendingCommand


def _get_row(whatsapp_number: str, session: Session) -> WhatsAppPendingCommand | None:
    return session.execute(
        select(WhatsAppPendingCommand).where(
            WhatsAppPendingCommand.whatsapp_number == whatsapp_number
        )
    ).scalar_one_or_none()


def set_command(whatsapp_number: str, command_key: str, session: Session) -> None:
    """Remember (or overwrite) the latest valid command for this number."""
    row = _get_row(whatsapp_number, session)
    if row is None:
        session.add(
            WhatsAppPendingCommand(whatsapp_number=whatsapp_number, command=command_key)
        )
    else:
        row.command = command_key
    session.flush()


def get_command(whatsapp_number: str, session: Session) -> str | None:
    """The canonical command key currently pending for this number, or None."""
    row = _get_row(whatsapp_number, session)
    return row.command if row is not None else None


def clear_command(whatsapp_number: str, session: Session) -> None:
    """Forget this number's pending command (a no-op if there is none)."""
    row = _get_row(whatsapp_number, session)
    if row is not None:
        session.delete(row)
        session.flush()
