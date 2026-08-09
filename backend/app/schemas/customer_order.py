from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from backend.app.schemas.types import IstDateTime


class CustomerOrderImportResultOut(BaseModel):
    order_id: int
    file_name: str
    status: str
    row_count: int
    error_count: int
    message: str | None = None


class CustomerOrderHistoryOut(BaseModel):
    id: int
    file_name: str
    status: str
    row_count: int
    error_count: int
    created_at: IstDateTime
    completed_at: IstDateTime | None = None


class CustomerOrderItemOut(BaseModel):
    id: int
    row_number: int
    part_number_raw: str
    quantity_requested: float


class CustomerOrderErrorOut(BaseModel):
    id: int
    row_number: int | None
    error_reason: str
    error_detail: str | None
    raw_row: dict | None
