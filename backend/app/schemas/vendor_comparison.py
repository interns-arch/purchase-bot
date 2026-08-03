from __future__ import annotations

from pydantic import BaseModel


class VendorComparisonRowOut(BaseModel):
    customer_part_number: str
    requested_quantity: float
    vendor_name: str | None
    vendor_id: int | None = None
    vendor_part_number: str | None
    part_description: str | None
    brand: str | None
    vendor_available_quantity: float | None
    mrp: float | None
    sale_price: float | None
    discount: str | None
    stock_status: str
    inventory_file: str | None
    order_item_id: int | None = None


class VendorComparisonSummaryOut(BaseModel):
    customer_order_items: int
    matched_items: int
    not_found_items: int
    invalid_items: int
    matching_vendors_found: int


class VendorComparisonOut(BaseModel):
    order_id: int
    order_file_name: str
    summary: VendorComparisonSummaryOut
    rows: list[VendorComparisonRowOut]
