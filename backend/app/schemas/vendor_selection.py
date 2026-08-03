from __future__ import annotations

from pydantic import BaseModel


class VendorSelectionIn(BaseModel):
    vendor_id: int
    quantity_selected: float


class VendorSelectionOut(BaseModel):
    id: int
    customer_order_item_id: int
    vendor_id: int
    vendor_name: str
    vendor_part_number: str
    quantity_selected: float
