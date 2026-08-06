from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class VendorInvoiceUploadResultOut(BaseModel):
    file_name: str
    invoice_import_id: int | None
    status: str
    vendor_id: int | None
    vendor_name: str | None
    row_count: int
    error_count: int
    message: str | None = None


class VendorInvoiceImportOut(BaseModel):
    id: int
    file_name: str
    status: str
    vendor_id: int | None
    vendor_name: str | None
    vendor_name_extracted: str | None
    row_count: int
    error_count: int
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None = None


class VendorInvoiceLineResultOut(BaseModel):
    id: int
    part_number_raw: str
    part_id: int | None
    quantity_invoiced: Decimal
    expected_quantity: Decimal | None
    discrepancy_type: str
    raw_text: str | None
