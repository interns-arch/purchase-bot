"""Customer Orders endpoints. Upload handling lives in
`backend.app.services.customer_order_service` (framework glue); the actual
import logic is `core.services.customer_order_service`, reused unchanged."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.models import User
from backend.app.database.session import get_db
from backend.app.schemas.customer_order import (
    CustomerOrderErrorOut,
    CustomerOrderHistoryOut,
    CustomerOrderImportResultOut,
    CustomerOrderItemOut,
)
from backend.app.services.customer_order_service import process_customer_order_uploads
from core.services import customer_order_service as order_service

router = APIRouter(
    prefix="/api/customer-orders", tags=["customer-orders"], dependencies=[Depends(get_current_user)]
)


@router.post("", response_model=list[CustomerOrderImportResultOut])
def upload_customer_orders(
    files: list[UploadFile],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomerOrderImportResultOut]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No files were uploaded."
        )

    outcomes = process_customer_order_uploads(files, db, sender=current_user.username)

    return [
        CustomerOrderImportResultOut(
            order_id=outcome.order_id or 0,
            file_name=outcome.file_name,
            status=outcome.status,
            row_count=outcome.row_count,
            error_count=outcome.error_count,
            message=outcome.message or outcome.error,
        )
        for outcome in outcomes
    ]


@router.get("", response_model=list[CustomerOrderHistoryOut])
def list_customer_orders(limit: int = 50, db: Session = Depends(get_db)) -> list[CustomerOrderHistoryOut]:
    rows = order_service.list_customer_order_history(db, limit=limit)
    return [
        CustomerOrderHistoryOut(
            id=row.id,
            file_name=row.file_name,
            status=row.status.value,
            row_count=row.row_count,
            error_count=row.error_count,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


@router.get("/{order_id}/items", response_model=list[CustomerOrderItemOut])
def list_customer_order_items(order_id: int, db: Session = Depends(get_db)) -> list[CustomerOrderItemOut]:
    order = order_service.get_customer_order(order_id, db)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer order not found.")

    return [
        CustomerOrderItemOut(
            id=item.id,
            row_number=item.row_number,
            part_number_raw=item.part_number_raw,
            quantity_requested=float(item.quantity_requested),
        )
        for item in order_service.list_customer_order_items(order_id, db)
    ]


@router.get("/{order_id}/errors", response_model=list[CustomerOrderErrorOut])
def list_customer_order_errors(order_id: int, db: Session = Depends(get_db)) -> list[CustomerOrderErrorOut]:
    return order_service.list_customer_order_errors(order_id, db)
