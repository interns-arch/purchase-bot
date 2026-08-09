from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from backend.app.schemas.types import IstDateTime


class IncomingDocumentOut(BaseModel):
    id: int
    source: str
    document_type: str
    filename: str
    sender: str | None
    status: str
    received_at: IstDateTime
    processed_at: IstDateTime | None
    error_message: str | None
