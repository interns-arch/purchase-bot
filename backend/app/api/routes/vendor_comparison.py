"""Vendor Comparison endpoints -- the heart of the application. Reuses the
existing matching engine (`core.services.vendor_comparison_service`)
unchanged; this layer only shapes the response and, for export, streams the
same workbook the CLI (`order_matching.py`) has always produced."""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.schemas.vendor_comparison import (
    VendorComparisonOut,
    VendorComparisonRowOut,
    VendorComparisonSummaryOut,
)
from core.services import customer_order_service as order_service
from core.services.vendor_comparison_service import compare_vendors_for_order, to_workbook

router = APIRouter(
    prefix="/api/vendor-comparison", tags=["vendor-comparison"], dependencies=[Depends(get_current_user)]
)


def _require_order(order_id: int, db: Session):
    order = order_service.get_customer_order(order_id, db)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer order not found.")
    return order


@router.get("/{order_id}", response_model=VendorComparisonOut)
def get_vendor_comparison(order_id: int, db: Session = Depends(get_db)) -> VendorComparisonOut:
    order = _require_order(order_id, db)
    result = compare_vendors_for_order(order_id, db)

    return VendorComparisonOut(
        order_id=order.id,
        order_file_name=order.file_name,
        summary=VendorComparisonSummaryOut(
            customer_order_items=result.summary.customer_order_items,
            matched_items=result.summary.matched_items,
            not_found_items=result.summary.not_found_items,
            invalid_items=result.summary.invalid_items,
            matching_vendors_found=result.summary.matching_vendors_found,
        ),
        rows=[
            VendorComparisonRowOut(
                customer_part_number=row.customer_part_number,
                requested_quantity=float(row.requested_quantity),
                vendor_name=row.vendor_name,
                vendor_id=row.vendor_id,
                vendor_part_number=row.vendor_part_number,
                part_description=row.part_description,
                brand=row.brand,
                vendor_available_quantity=(
                    float(row.vendor_available_quantity)
                    if row.vendor_available_quantity is not None
                    else None
                ),
                mrp=float(row.mrp) if row.mrp is not None else None,
                sale_price=float(row.sale_price) if row.sale_price is not None else None,
                discount=row.discount,
                stock_status=row.stock_status,
                inventory_file=row.inventory_file,
                order_item_id=row.order_item_id,
            )
            for row in result.rows
        ],
    )


@router.get("/{order_id}/export")
def export_vendor_comparison(order_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    order = _require_order(order_id, db)
    result = compare_vendors_for_order(order_id, db)
    workbook = to_workbook(result.rows)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    file_name = f"vendor_comparison_order_{order.id}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
