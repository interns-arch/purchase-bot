"""Delivery Tracking dashboard endpoint. Thin wrapper over
`core.services.delivery_tracking_service`; no business rules live here."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.schemas.delivery_tracking import (
    DailyDeliveryPointOut,
    DeliveryTrackingOut,
    DeliveryTrackingRowOut,
    DeliveryTrackingSummaryOut,
    VendorDeliveryPointOut,
)
from core.services import delivery_tracking_service

router = APIRouter(
    prefix="/api/delivery-tracking",
    tags=["delivery-tracking"],
    dependencies=[Depends(get_current_user)],
)


def _row_out(row: delivery_tracking_service.DeliveryTrackingRow) -> DeliveryTrackingRowOut:
    return DeliveryTrackingRowOut(
        vendor_id=row.vendor_id,
        vendor_name=row.vendor_name,
        part_id=row.part_id,
        part_number=row.part_number,
        ordered_qty=float(row.ordered_qty),
        delivered_qty=float(row.delivered_qty),
        short_qty=float(row.short_qty),
        status=row.status,
        last_delivery_date=row.last_delivery_date,
    )


@router.get("", response_model=DeliveryTrackingOut)
def get_delivery_tracking(
    date_from: date | None = None,
    date_to: date | None = None,
    vendor_id: int | None = None,
    customer_order_id: int | None = None,
    part_number: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> DeliveryTrackingOut:
    rows = delivery_tracking_service.compute_rows(
        db,
        date_from=date_from,
        date_to=date_to,
        vendor_id=vendor_id,
        customer_order_id=customer_order_id,
        part_number=part_number,
        status=status,
    )
    summary = delivery_tracking_service.compute_summary(rows)
    daily = delivery_tracking_service.compute_daily_deliveries(
        db, vendor_id=vendor_id, date_from=date_from, date_to=date_to
    )
    vendorwise = delivery_tracking_service.compute_vendorwise_deliveries(rows)

    return DeliveryTrackingOut(
        rows=[_row_out(row) for row in rows],
        summary=DeliveryTrackingSummaryOut(
            total_ordered_qty=float(summary.total_ordered_qty),
            total_delivered_qty=float(summary.total_delivered_qty),
            total_short_qty=float(summary.total_short_qty),
            complete_count=summary.complete_count,
            partial_count=summary.partial_count,
            not_delivered_count=summary.not_delivered_count,
        ),
        daily_deliveries=[
            DailyDeliveryPointOut(delivery_date=point.delivery_date, delivered_qty=float(point.delivered_qty))
            for point in daily
        ],
        vendorwise_deliveries=[
            VendorDeliveryPointOut(vendor_name=point.vendor_name, delivered_qty=float(point.delivered_qty))
            for point in vendorwise
        ],
    )
