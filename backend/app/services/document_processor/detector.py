"""Classifies an incoming file into an `IncomingDocumentType`. Manual
uploads always carry an explicit `document_type_hint` (the endpoint the
user clicked already says which one it is) and skip straight past every
*document-type* heuristic below -- but a manual Vendor Inventory upload's
*vendor* still needs to be resolved from its filename's Vendor Code, exactly
like a WhatsApp upload, so that check runs either way (see
`_classify_inventory`). WhatsApp uploads have no hint at all, so document
type itself also falls back to: vendor-code-shaped filename prefix ->
caption keyword override -> default-to-customer-order -> UNKNOWN (human
review) if nothing resolves.

Vendor Inventory and Customer Order files are NOT distinguishable by column
headers alone (both only require Part Number + Quantity per
`core.ingestion.column_detector.find_required_columns`) -- that's exactly
why the vendor code, not file structure, is the primary signal here.
Sender identity is deliberately NEVER used to identify a vendor: every
vendor messages the same shared WhatsApp Business number, so a sender's
phone number cannot tell them apart (see `core.services.vendor_code_service`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.documents.models import IncomingDocumentType
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.validator import INVOICE_EXTENSIONS
from core.services import vendor_code_service

_CAPTION_KEYWORDS: dict[str, IncomingDocumentType] = {
    "inventory": IncomingDocumentType.VENDOR_INVENTORY,
    "stock": IncomingDocumentType.VENDOR_INVENTORY,
    "order": IncomingDocumentType.CUSTOMER_ORDER,
    "invoice": IncomingDocumentType.VENDOR_INVOICE,
}


@dataclass
class Classification:
    document_type: IncomingDocumentType
    vendor_id: int | None = None
    # Set when the filename has a vendor-code-shaped prefix (e.g. "AR_CT")
    # regardless of whether it resolved to a real vendor -- `vendor_id` is
    # None + this set means "code present but not registered", which the
    # dispatcher rejects with a clear error instead of silently treating the
    # file as a customer order.
    vendor_code: str | None = None


def _keyword_override(caption: str | None) -> IncomingDocumentType | None:
    if not caption:
        return None
    text = caption.lower()
    for keyword, document_type in _CAPTION_KEYWORDS.items():
        if keyword in text:
            return document_type
    return None


def _classify_inventory(file_path: Path, session: Session) -> Classification:
    """Resolves the vendor for a file already known to be Vendor Inventory
    (whether by manual-upload hint or by heuristic below) from its
    filename's Vendor Code -- applies identically to manual and WhatsApp
    uploads, since a returning vendor must be identifiable the same way
    either way."""
    code = vendor_code_service.parse_vendor_code_from_filename(file_path.name)
    if code is None:
        return Classification(IncomingDocumentType.VENDOR_INVENTORY)

    vendor = vendor_code_service.get_vendor_by_code(code, session)
    return Classification(
        IncomingDocumentType.VENDOR_INVENTORY,
        vendor.id if vendor is not None else None,
        vendor_code=code,
    )


def classify(file_path: Path, metadata: DocumentMetadata, session: Session) -> Classification:
    if file_path.suffix.lower() in INVOICE_EXTENSIONS:
        return Classification(IncomingDocumentType.VENDOR_INVOICE)

    if metadata.document_type_hint is not None:
        if (
            metadata.document_type_hint == IncomingDocumentType.VENDOR_INVENTORY
            and metadata.vendor_id_hint is None
        ):
            return _classify_inventory(file_path, session)
        return Classification(metadata.document_type_hint, metadata.vendor_id_hint)

    code_classification = _classify_inventory(file_path, session)
    if code_classification.vendor_code is not None:
        return code_classification

    caption_override = _keyword_override(metadata.caption)
    if caption_override is not None:
        return Classification(caption_override)

    if metadata.sender:
        return Classification(IncomingDocumentType.CUSTOMER_ORDER)

    return Classification(IncomingDocumentType.UNKNOWN)
