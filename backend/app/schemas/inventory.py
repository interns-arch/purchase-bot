from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from backend.app.schemas.types import IstDateTime


class ImportResultOut(BaseModel):
    import_id: int
    vendor_id: int
    vendor_name: str
    file_name: str
    status: str
    is_duplicate: bool
    row_count: int
    error_count: int
    message: str | None = None


class ImportHistoryOut(BaseModel):
    id: int
    vendor_id: int
    vendor_name: str
    vendor_code: str | None = None
    file_name: str
    status: str
    is_active: bool
    row_count: int
    error_count: int
    created_at: IstDateTime
    completed_at: IstDateTime | None = None


class ImportErrorOut(BaseModel):
    id: int
    row_number: int | None
    error_reason: str
    error_detail: str | None
    raw_row: dict | None
