"""Purchase Order endpoints -- history/list and manual download. Purchase
Orders themselves are only ever generated automatically (after Vendor
Selection, see `backend/app/api/routes/vendor_selection.py`), never
uploaded, so there's no POST/create route here. Thin wrapper over
`core.services.purchase_order_generation_service`; no business rules live
here."""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.core.config import settings
from backend.app.database.session import get_db
from backend.app.schemas.purchase_order import PurchaseOrderLineOut, PurchaseOrderOut
from core.services import purchase_order_generation_service as po_service

router = APIRouter(
    prefix="/api/purchase-orders", tags=["purchase-orders"], dependencies=[Depends(get_current_user)]
)


def _to_out(po) -> PurchaseOrderOut:
    return PurchaseOrderOut(
        id=po.id,
        po_number=po.po_number,
        vendor_id=po.vendor_id,
        vendor_name=po.vendor.name,
        vendor_code=po.vendor.vendor_code,
        customer_order_id=po.customer_order_id,
        status=po.status.value,
        created_at=po.created_at,
        emailed_at=po.emailed_at,
    )


@router.get("", response_model=list[PurchaseOrderOut])
def list_purchase_orders(
    order_id: int | None = None, db: Session = Depends(get_db)
) -> list[PurchaseOrderOut]:
    rows = po_service.list_purchase_orders(db, order_id=order_id)
    return [_to_out(row) for row in rows]


@router.get("/{po_id}/export")
def export_purchase_order(po_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    po = po_service.get_purchase_order(po_id, db)
    if po is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase order not found.")

    lines = po_service.list_purchase_order_lines(po_id, db)
    workbook = po_service.to_export_workbook(
        po,
        lines,
        company_name=settings.company_name,
        company_address=settings.company_address,
        company_phone=settings.company_phone,
        company_email=settings.company_email,
    )

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    file_name = f"{po.po_number}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@router.post("/{po_id}/resend-email", response_model=PurchaseOrderOut)
def resend_purchase_order_email(po_id: int, db: Session = Depends(get_db)) -> PurchaseOrderOut:
    """Manually re-send this PO's internal review email (never to a vendor).
    Useful when the automatic send hit a transient failure (EMAIL_FAILED).
    Updates only this PO's own status; a fresh failure just records
    EMAIL_FAILED again and returns the PO -- it never raises to the client
    for a mail outage, only for missing configuration."""
    try:
        po = po_service.resend_purchase_order_email(po_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if po is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase order not found.")

    return _to_out(po)


@router.get("/{po_id}/lines", response_model=list[PurchaseOrderLineOut])
def list_purchase_order_lines(po_id: int, db: Session = Depends(get_db)) -> list[PurchaseOrderLineOut]:
    if po_service.get_purchase_order(po_id, db) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase order not found.")

    lines = po_service.list_purchase_order_lines(po_id, db)
    return [
        PurchaseOrderLineOut(
            part_number=line.part_number,
            vendor_part_number=line.vendor_part_number,
            quantity=line.quantity,
        )
        for line in lines
    ]
