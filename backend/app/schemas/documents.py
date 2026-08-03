from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class IncomingDocumentOut(BaseModel):
    id: int
    source: str
    document_type: str
    filename: str
    sender: str | None
    status: str
    received_at: datetime
    processed_at: datetime | None
    error_message: str | None
