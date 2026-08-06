"""Vendor Invoice Verification endpoints (manual upload + automation share
this same underlying pipeline -- see `app.services.invoice_service` for the
manual glue and `backend.app.workers.document_worker` /
`backend.app.integrations.whatsapp` for the automated paths). Upload
handling lives in `app.services.invoice_service`; the actual extraction +
verification logic is `core.services.vendor_invoice_verification_service`,
reused unchanged."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.models import User
from backend.app.database.session import get_db
from backend.app.schemas.vendor_invoice import (
    VendorInvoiceImportOut,
    VendorInvoiceLineResultOut,
    VendorInvoiceUploadResultOut,
)
from backend.app.services.invoice_service import process_invoice_uploads
from core.services import vendor_invoice_verification_service as invoice_service

router = APIRouter(
    prefix="/api/vendor-invoices", tags=["vendor-invoices"], dependencies=[Depends(get_current_user)]
)


@router.post("/imports", response_model=list[VendorInvoiceUploadResultOut])
def upload_invoice_files(
    files: list[UploadFile],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VendorInvoiceUploadResultOut]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No files were uploaded."
        )

    outcomes = process_invoice_uploads(files, db, sender=current_user.username)
    return [
        VendorInvoiceUploadResultOut(
            file_name=outcome.file_name,
            invoice_import_id=outcome.invoice_import_id,
            status=outcome.status,
            vendor_id=outcome.vendor_id,
            vendor_name=outcome.vendor_name,
            row_count=outcome.row_count,
            error_count=outcome.error_count,
            message=outcome.message,
        )
        for outcome in outcomes
    ]


@router.get("/imports", response_model=list[VendorInvoiceImportOut])
def list_invoice_imports(db: Session = Depends(get_db)) -> list[VendorInvoiceImportOut]:
    rows = invoice_service.list_invoice_imports(db)
    return [
        VendorInvoiceImportOut(
            id=row.id,
            file_name=row.file_name,
            status=row.status.value,
            vendor_id=row.vendor_id,
            vendor_name=row.vendor.name if row.vendor else None,
            vendor_name_extracted=row.vendor_name_extracted,
            row_count=row.row_count,
            error_count=row.error_count,
            error_message=row.error_message,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


@router.get("/imports/{invoice_import_id}/lines", response_model=list[VendorInvoiceLineResultOut])
def list_invoice_lines(
    invoice_import_id: int, db: Session = Depends(get_db)
) -> list[VendorInvoiceLineResultOut]:
    if invoice_service.get_invoice_import(invoice_import_id, db) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice import not found.")

    rows = invoice_service.list_invoice_line_results(invoice_import_id, db)
    return [
        VendorInvoiceLineResultOut(
            id=row.id,
            part_number_raw=row.part_number_raw,
            part_id=row.part_id,
            quantity_invoiced=row.quantity_invoiced,
            expected_quantity=row.expected_quantity,
            discrepancy_type=row.discrepancy_type.value,
            raw_text=row.raw_text,
        )
        for row in rows
    ]
