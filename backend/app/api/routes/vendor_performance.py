"""Vendor Performance dashboard endpoints. Built entirely on top of
`core.services.vendor_performance_tracking_service`, which itself
aggregates `delivery_tracking_service`'s (vendor, part) reconciliation --
never re-derives ordered/delivered/short quantities. Computed fresh on
every request, same "never cache" convention used throughout this app."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.schemas.delivery_tracking import DeliveryTrackingRowOut
from backend.app.schemas.vendor_performance import (
    MonthlyTrendPointOut,
    VendorDetailDeliveryOut,
    VendorDetailOut,
    VendorDetailSelectionOut,
    VendorPerformanceOut,
    VendorPerformanceRowOut,
    VendorPerformanceSummaryOut,
)
from core.services import vendor_performance_tracking_service as performance_service

router = APIRouter(
    prefix="/api/vendor-performance",
    tags=["vendor-performance"],
    dependencies=[Depends(get_current_user)],
)


def _row_out(row: performance_service.VendorPerformanceRow) -> VendorPerformanceRowOut:
    return VendorPerformanceRowOut(
        vendor_id=row.vendor_id,
        vendor_name=row.vendor_name,
        parts_allocated=row.parts_allocated,
        ordered_qty=float(row.ordered_qty),
        delivered_qty=float(row.delivered_qty),
        short_qty=float(row.short_qty),
        fulfillment_pct=float(row.fulfillment_pct),
        accuracy_pct=float(row.accuracy_pct),
    )


def _delivery_row_out(row) -> DeliveryTrackingRowOut:
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


@router.get("", response_model=VendorPerformanceOut)
def get_vendor_performance(
    date_from: date | None = None,
    date_to: date | None = None,
    vendor_id: int | None = None,
    customer_order_id: int | None = None,
    part_number: str | None = None,
    db: Session = Depends(get_db),
) -> VendorPerformanceOut:
    rows = performance_service.compute_vendor_rows(
        db,
        date_from=date_from,
        date_to=date_to,
        vendor_id=vendor_id,
        customer_order_id=customer_order_id,
        part_number=part_number,
    )
    summary = performance_service.compute_summary(rows, db)
    trend = performance_service.compute_monthly_trend(db, vendor_id=vendor_id)

    return VendorPerformanceOut(
        rows=[_row_out(row) for row in rows],
        summary=VendorPerformanceSummaryOut(
            best_vendor_name=summary.best_vendor_name,
            best_vendor_fulfillment_pct=(
                float(summary.best_vendor_fulfillment_pct)
                if summary.best_vendor_fulfillment_pct is not None
                else None
            ),
            lowest_vendor_name=summary.lowest_vendor_name,
            lowest_vendor_fulfillment_pct=(
                float(summary.lowest_vendor_fulfillment_pct)
                if summary.lowest_vendor_fulfillment_pct is not None
                else None
            ),
            average_fulfillment_pct=float(summary.average_fulfillment_pct),
            total_vendors=summary.total_vendors,
            total_deliveries=summary.total_deliveries,
            total_short_qty=float(summary.total_short_qty),
        ),
        monthly_trend=[
            MonthlyTrendPointOut(
                month=point.month,
                delivered_qty=float(point.delivered_qty),
                accuracy_pct=float(point.accuracy_pct),
            )
            for point in trend
        ],
    )


@router.get("/{vendor_id}", response_model=VendorDetailOut)
def get_vendor_detail(vendor_id: int, db: Session = Depends(get_db)) -> VendorDetailOut:
    try:
        detail = performance_service.compute_vendor_detail(vendor_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return VendorDetailOut(
        vendor_id=detail.vendor_id,
        vendor_name=detail.vendor_name,
        performance=_row_out(detail.performance) if detail.performance else None,
        delivery_rows=[_delivery_row_out(row) for row in detail.delivery_rows],
        selections=[
            VendorDetailSelectionOut(
                customer_order_id=selection.customer_order_id,
                customer_order_file_name=selection.customer_order_file_name,
                part_number=selection.part_number,
                quantity_selected=float(selection.quantity_selected),
                selected_at=selection.selected_at,
            )
            for selection in detail.selections
        ],
        deliveries=[
            VendorDetailDeliveryOut(
                part_number=delivery.part_number,
                quantity_delivered=float(delivery.quantity_delivered),
                delivery_date=delivery.delivery_date,
                file_name=delivery.file_name,
            )
            for delivery in detail.deliveries
        ],
    )
