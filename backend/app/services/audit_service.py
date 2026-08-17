"""Append-only audit recording for MANUAL, management-impacting actions
(Founder spec §24). One tiny function so call sites stay one line; recording
must never break the action being audited."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.logging_setup import get_logger
from core.models import AuditLog

logger = get_logger(__name__)


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    previous_value=None,
    new_value=None,
    reason: str | None = None,
) -> None:
    """Write one audit row on the CALLER'S session/transaction, so the audit
    commits (or rolls back) atomically with the action itself. Never raises."""
    try:
        session.add(
            AuditLog(
                actor=actor,
                action=action,
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id is not None else None,
                previous_value=_as_text(previous_value),
                new_value=_as_text(new_value),
                reason=reason,
            )
        )
        session.flush()
    except Exception:  # noqa: BLE001 -- auditing must never break the audited action
        logger.exception("Could not record audit row for action %r.", action)


def list_recent(session: Session, *, limit: int = 100) -> list[AuditLog]:
    return list(
        session.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        ).scalars()
    )
