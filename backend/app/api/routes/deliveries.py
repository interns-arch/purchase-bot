"""Delivery Upload endpoints (Delivery Tracking module). Upload handling
lives in `app.services.delivery_service` (framework glue); the actual
import logic is `core.services.vendor_delivery_service` -- deliveries are
matched by vendor + part directly, since this application has no Purchase
Order concept."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.models import User
from backend.app.database.session import get_db
from backend.app.schemas.delivery import (
    DeliveryImportErrorOut,
    DeliveryImportHistoryOut,
    DeliveryImportResultOut,
)
from backend.app.services.delivery_service import process_delivery_uploads
from core.services import vendor_delivery_service as delivery_service

router = APIRouter(
    prefix="/api/deliveries", tags=["deliveries"], dependencies=[Depends(get_current_user)]
)


@router.post("/imports", response_model=list[DeliveryImportResultOut])
def upload_delivery_files(
    files: list[UploadFile],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DeliveryImportResultOut]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No files were uploaded."
        )

    outcomes = process_delivery_uploads(files, db, sender=current_user.username)
    return [
        DeliveryImportResultOut(
            import_id=outcome.import_id,
            file_name=outcome.file_name,
            status=outcome.status,
            row_count=outcome.row_count,
            error_count=outcome.error_count,
            message=outcome.message,
        )
        for outcome in outcomes
    ]


@router.get("/imports", response_model=list[DeliveryImportHistoryOut])
def list_delivery_imports(
    limit: int = 50, db: Session = Depends(get_db)
) -> list[DeliveryImportHistoryOut]:
    rows = delivery_service.list_vendor_delivery_import_history(db)[:limit]
    return [
        DeliveryImportHistoryOut(
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


@router.get("/imports/{import_id}/errors", response_model=list[DeliveryImportErrorOut])
def list_delivery_import_errors(
    import_id: int, db: Session = Depends(get_db)
) -> list[DeliveryImportErrorOut]:
    return delivery_service.list_vendor_delivery_import_errors(import_id, db)
