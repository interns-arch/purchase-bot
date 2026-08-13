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
    # True when this row's Download button can produce something: either the
    # import stored rows (exported as a one-vendor workbook) or the ORIGINAL
    # uploaded file is still on the server (the case that matters for FAILED
    # imports -- see `GET /api/inventory/imports/{id}/export`).
    can_download: bool = False


class ImportErrorOut(BaseModel):
    id: int
    row_number: int | None
    error_reason: str
    error_detail: str | None
    raw_row: dict | None
