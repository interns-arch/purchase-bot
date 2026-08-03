from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from backend.app.schemas.delivery_tracking import DeliveryTrackingRowOut


class VendorPerformanceRowOut(BaseModel):
    vendor_id: int
    vendor_name: str
    parts_allocated: int
    ordered_qty: float
    delivered_qty: float
    short_qty: float
    fulfillment_pct: float
    accuracy_pct: float


class VendorPerformanceSummaryOut(BaseModel):
    best_vendor_name: str | None
    best_vendor_fulfillment_pct: float | None
    lowest_vendor_name: str | None
    lowest_vendor_fulfillment_pct: float | None
    average_fulfillment_pct: float
    total_vendors: int
    total_deliveries: int
    total_short_qty: float


class MonthlyTrendPointOut(BaseModel):
    month: str
    delivered_qty: float
    accuracy_pct: float


class VendorPerformanceOut(BaseModel):
    rows: list[VendorPerformanceRowOut]
    summary: VendorPerformanceSummaryOut
    monthly_trend: list[MonthlyTrendPointOut]


class VendorDetailSelectionOut(BaseModel):
    customer_order_id: int
    customer_order_file_name: str
    part_number: str
    quantity_selected: float
    selected_at: datetime


class VendorDetailDeliveryOut(BaseModel):
    part_number: str
    quantity_delivered: float
    delivery_date: date | None
    file_name: str


class VendorDetailOut(BaseModel):
    vendor_id: int
    vendor_name: str
    performance: VendorPerformanceRowOut | None
    delivery_rows: list[DeliveryTrackingRowOut]
    selections: list[VendorDetailSelectionOut]
    deliveries: list[VendorDetailDeliveryOut]
