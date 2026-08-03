from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class DeliveryTrackingRowOut(BaseModel):
    vendor_id: int
    vendor_name: str
    part_id: int
    part_number: str
    ordered_qty: float
    delivered_qty: float
    short_qty: float
    status: str
    last_delivery_date: date | None


class DeliveryTrackingSummaryOut(BaseModel):
    total_ordered_qty: float
    total_delivered_qty: float
    total_short_qty: float
    complete_count: int
    partial_count: int
    not_delivered_count: int


class DailyDeliveryPointOut(BaseModel):
    delivery_date: date
    delivered_qty: float


class VendorDeliveryPointOut(BaseModel):
    vendor_name: str
    delivered_qty: float


class DeliveryTrackingOut(BaseModel):
    rows: list[DeliveryTrackingRowOut]
    summary: DeliveryTrackingSummaryOut
    daily_deliveries: list[DailyDeliveryPointOut]
    vendorwise_deliveries: list[VendorDeliveryPointOut]
