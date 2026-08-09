from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from backend.app.schemas.types import IstDateTime


class RecentActivityOut(BaseModel):
    activity_type: str
    reference_id: int
    label: str
    file_name: str
    status: str
    row_count: int
    error_count: int
    created_at: IstDateTime


class DashboardOut(BaseModel):
    active_vendors: int
    total_vendors: int
    total_inventory_imports: int
    total_customer_orders: int
    parts_matched: int
    parts_not_found: int
    last_import_at: IstDateTime | None = None
    recent_activity: list[RecentActivityOut]
