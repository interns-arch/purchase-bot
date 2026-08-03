"""Vendor Selection endpoints -- lets the user pick one vendor + quantity
per order line from the Vendor Comparison report, and export the resulting
per-part vendor allocation as an Excel sheet. Thin wrapper over
`core.services.vendor_selection_service`; no business rules live here."""

from __future__ import annotations

import io
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.schemas.vendor_selection import VendorSelectionIn, VendorSelectionOut
from core.models import VendorSelection
from core.services import vendor_selection_service

router = APIRouter(
    prefix="/api/vendor-selection",
    tags=["vendor-selection"],
    dependencies=[Depends(get_current_user)],
)


def _to_out(selection: VendorSelection) -> VendorSelectionOut:
    return VendorSelectionOut(
        id=selection.id,
        customer_order_item_id=selection.customer_order_item_id,
        vendor_id=selection.vendor_id,
        vendor_name=selection.vendor.name,
        vendor_part_number=selection.vendor_part_number,
        quantity_selected=float(selection.quantity_selected),
    )


@router.get("/{order_id}", response_model=list[VendorSelectionOut])
def list_selections(order_id: int, db: Session = Depends(get_db)) -> list[VendorSelectionOut]:
    selections = vendor_selection_service.list_selections_for_order(order_id, db)
    return [_to_out(selection) for selection in selections]


@router.put("/{order_id}/items/{order_item_id}", response_model=VendorSelectionOut)
def select_vendor(
    order_id: int,
    order_item_id: int,
    payload: VendorSelectionIn,
    db: Session = Depends(get_db),
) -> VendorSelectionOut:
    try:
        selection = vendor_selection_service.upsert_selection(
            order_item_id,
            payload.vendor_id,
            Decimal(str(payload.quantity_selected)),
            db,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return _to_out(selection)


@router.get("/{order_id}/export")
def export_selected_vendors(order_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    rows = vendor_selection_service.list_selections_for_export(order_id, db)
    workbook = vendor_selection_service.to_export_workbook(rows)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    file_name = f"selected_vendors_order_{order_id}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
