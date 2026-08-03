"""Persisted state for the Integration Status page -- a single row tracking
the last webhook verification and the last live connection test, so the
admin can see current integration health without re-triggering anything.
Shares `core.models.Base`/the same database, same reasoning as
`backend/app/documents/models.py`: this is a web-app/integration concept,
not core business logic."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base

STATUS_ROW_ID = 1


class WhatsAppIntegrationStatus(Base):
    __tablename__ = "whatsapp_integration_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_webhook_verified_at: Mapped[datetime | None] = mapped_column(default=None)
    last_connection_tested_at: Mapped[datetime | None] = mapped_column(default=None)
    last_connection_success: Mapped[bool | None] = mapped_column(default=None)
    last_connection_message: Mapped[str | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
