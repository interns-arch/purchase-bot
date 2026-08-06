"""Persisted state for the Integration Status page's Gmail panel -- mirrors
`backend/app/integrations/whatsapp/models.py`'s singleton-row pattern."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base

STATUS_ROW_ID = 1


class GmailIntegrationStatus(Base):
    __tablename__ = "gmail_integration_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(default=None)
    last_poll_success: Mapped[bool | None] = mapped_column(default=None)
    last_poll_message: Mapped[str | None] = mapped_column(default=None)
    last_message_processed_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
