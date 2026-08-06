"""Inventory Import endpoints. Upload handling lives in
`app.services.inventory_service` (framework glue); the actual import logic
is `core.services.inventory_import_service`, reused unchanged."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.models import User
from backend.app.database.session import get_db
from backend.app.schemas.inventory import ImportErrorOut, ImportHistoryOut, ImportResultOut
from backend.app.services.inventory_service import process_uploads
from core.services import inventory_import_service as import_service
from core.services import vendor_service

router = APIRouter(
    prefix="/api/inventory", tags=["inventory"], dependencies=[Depends(get_current_user)]
)


@router.post("/imports", response_model=list[ImportResultOut])
def upload_inventory_files(
    files: list[UploadFile],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ImportResultOut]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No files were uploaded."
        )

    outcomes = process_uploads(files, db, sender=current_user.username)

    results: list[ImportResultOut] = []
    for outcome in outcomes:
        if outcome.error:
            results.append(
                ImportResultOut(
                    import_id=outcome.import_id or 0,
                    vendor_id=outcome.vendor_id or 0,
                    vendor_name=outcome.vendor_name or "-",
                    file_name=outcome.file_name,
                    status=outcome.status,
                    is_duplicate=outcome.is_duplicate,
                    row_count=outcome.row_count,
                    error_count=outcome.error_count,
                    message=outcome.error,
                )
            )
        else:
            results.append(
                ImportResultOut(
                    import_id=outcome.import_id,
                    vendor_id=outcome.vendor_id,
                    vendor_name=outcome.vendor_name,
                    file_name=outcome.file_name,
                    status=outcome.status,
                    is_duplicate=outcome.is_duplicate,
                    row_count=outcome.row_count,
                    error_count=outcome.error_count,
                    message=outcome.message,
                )
            )
    return results


@router.get("/imports", response_model=list[ImportHistoryOut])
def list_imports(
    vendor_id: int | None = None, limit: int = 50, db: Session = Depends(get_db)
) -> list[ImportHistoryOut]:
    if vendor_id is not None:
        rows = import_service.list_import_history(vendor_id, db)[:limit]
    else:
        rows = import_service.list_all_import_history(db, limit=limit)

    return [
        ImportHistoryOut(
            id=row.id,
            vendor_id=row.vendor_id,
            vendor_name=row.vendor.name,
            vendor_code=row.vendor.vendor_code,
            file_name=row.file_name,
            status=row.status.value,
            is_active=row.is_active,
            row_count=row.row_count,
            error_count=row.error_count,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


@router.get("/imports/{import_id}/errors", response_model=list[ImportErrorOut])
def list_import_errors(import_id: int, db: Session = Depends(get_db)) -> list[ImportErrorOut]:
    return import_service.list_import_errors(import_id, db)


@router.post("/imports/{import_id}/confirm", response_model=ImportResultOut)
def confirm_import(import_id: int, db: Session = Depends(get_db)) -> ImportResultOut:
    try:
        result = import_service.confirm_import(import_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except import_service.InvalidStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    vendor = vendor_service.get_vendor(result.vendor_id, db)
    return ImportResultOut(
        import_id=result.import_id,
        vendor_id=result.vendor_id,
        vendor_name=vendor.name,
        file_name="",
        status=result.status.value,
        is_duplicate=result.is_duplicate,
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
    )


@router.post("/imports/{import_id}/cancel", response_model=ImportResultOut)
def cancel_import(import_id: int, db: Session = Depends(get_db)) -> ImportResultOut:
    try:
        result = import_service.cancel_import(import_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except import_service.InvalidStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    vendor = vendor_service.get_vendor(result.vendor_id, db)
    return ImportResultOut(
        import_id=result.import_id,
        vendor_id=result.vendor_id,
        vendor_name=vendor.name,
        file_name="",
        status=result.status.value,
        is_duplicate=result.is_duplicate,
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
    )
