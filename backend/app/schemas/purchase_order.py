from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class PurchaseOrderOut(BaseModel):
    id: int
    po_number: str
    vendor_id: int
    vendor_name: str
    vendor_code: str | None
    customer_order_id: int
    status: str
    created_at: datetime
    emailed_at: datetime | None = None


class PurchaseOrderLineOut(BaseModel):
    part_number: str
    vendor_part_number: str
    quantity: Decimal
