"""Classifies an incoming file into an `IncomingDocumentType`. Manual
uploads always carry an explicit `document_type_hint` (the endpoint the
user clicked already says which one it is) and skip straight past every
heuristic below. WhatsApp uploads have no such hint, so classification
falls back to: caption keyword override -> sender identity (registered
vendor WhatsApp number) -> default-to-customer-order -> UNKNOWN (human
review) if nothing resolves at all.

Vendor Inventory and Customer Order files are NOT distinguishable by column
headers alone (both only require Part Number + Quantity per
`core.ingestion.column_detector.find_required_columns`) -- that's exactly
why sender identity, not file structure, is the primary signal here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.documents.models import IncomingDocumentType
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.validator import INVOICE_EXTENSIONS
from core.services import vendor_service

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


def _keyword_override(caption: str | None) -> IncomingDocumentType | None:
    if not caption:
        return None
    text = caption.lower()
    for keyword, document_type in _CAPTION_KEYWORDS.items():
        if keyword in text:
            return document_type
    return None


def classify(file_path: Path, metadata: DocumentMetadata, session: Session) -> Classification:
    if file_path.suffix.lower() in INVOICE_EXTENSIONS:
        return Classification(IncomingDocumentType.VENDOR_INVOICE)

    if metadata.document_type_hint is not None:
        return Classification(metadata.document_type_hint, metadata.vendor_id_hint)

    caption_override = _keyword_override(metadata.caption)
    if caption_override is not None:
        vendor_id = None
        if caption_override == IncomingDocumentType.VENDOR_INVENTORY and metadata.sender:
            vendor = vendor_service.get_vendor_by_whatsapp_number(metadata.sender, session)
            vendor_id = vendor.id if vendor else None
        return Classification(caption_override, vendor_id)

    if metadata.sender:
        vendor = vendor_service.get_vendor_by_whatsapp_number(metadata.sender, session)
        if vendor is not None:
            return Classification(IncomingDocumentType.VENDOR_INVENTORY, vendor.id)
        return Classification(IncomingDocumentType.CUSTOMER_ORDER)

    return Classification(IncomingDocumentType.UNKNOWN)
