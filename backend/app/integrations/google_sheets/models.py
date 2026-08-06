"""Persisted state for the Integration Status page's Google Sheets panel --
mirrors `backend/app/integrations/whatsapp/models.py`'s singleton-row
pattern."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base

STATUS_ROW_ID = 1


class GoogleSheetsIntegrationStatus(Base):
    __tablename__ = "google_sheets_integration_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(default=None)
    last_sync_success: Mapped[bool | None] = mapped_column(default=None)
    last_sync_message: Mapped[str | None] = mapped_column(default=None)
    last_synced_vendor_name: Mapped[str | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
