"""Classifies an incoming file into an `IncomingDocumentType`.

The channel a document arrived on is authoritative (business workflow), so
`classify()` routes by `source` first and only MANUAL uploads reach the
heuristic classifier:

- WHATSAPP -> ALWAYS Vendor Inventory. The vendor is still resolved from the
  filename's Vendor Code (see `_classify_inventory`) so the AR_CT.xlsx /
  BI_CT.xlsx onboarding workflow is unchanged; an unregistered code is still
  rejected downstream rather than silently misrouted.
- EMAIL (dedicated Gmail inbox) -> Customer Order for spreadsheets, Vendor
  Invoice for PDF purchase bills -- decided purely by file format.
- MANUAL -> `_classify_manual`: the explicit `document_type_hint` from the
  endpoint the user clicked (a manual Vendor Inventory upload's *vendor* is
  still resolved from its filename's Vendor Code, exactly like WhatsApp).

Vendor Inventory and Customer Order files are NOT distinguishable by column
headers alone (both only require Part Number + Quantity per
`core.ingestion.column_detector.find_required_columns`) -- which is why
source, not file structure, is the primary signal. Sender identity is
deliberately NEVER used to identify a vendor: every vendor messages the same
shared WhatsApp Business number, so a sender's phone number cannot tell them
apart (see `core.services.vendor_code_service`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.documents.models import DocumentSource, IncomingDocumentType
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.validator import INVOICE_EXTENSIONS
from core.logging_setup import get_logger
from core.services import vendor_code_service

logger = get_logger(__name__)

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


def classify(
    file_path: Path, metadata: DocumentMetadata, session: Session, *, source: DocumentSource
) -> Classification:
    """Decide a document's type. The channel it arrived on is authoritative
    per the business workflow, so WhatsApp and Gmail bypass the heuristic
    classifier entirely -- only MANUAL uploads are classified:

    - WHATSAPP -> ALWAYS Vendor Inventory. The vendor is still resolved from
      the filename's Vendor Code (AR_CT.xlsx / BI_CT.xlsx ...) so the
      vendor-code onboarding workflow is unaffected.
    - EMAIL (dedicated Gmail inbox) -> Customer Order for spreadsheets, or
      Vendor Invoice for PDF purchase bills, decided purely by file format.
    - MANUAL -> the hint/heuristic classifier below (`_classify_manual`)."""
    if source == DocumentSource.WHATSAPP:
        # Force Vendor Inventory, but keep the filename Vendor-Code lookup so a
        # returning vendor is still identified (and an unknown code is still
        # rejected downstream) exactly as before.
        classification = _classify_inventory(file_path, session)
        logger.info(
            "Forcing document_type=VENDOR_INVENTORY for WHATSAPP document '%s' "
            "(vendor_code=%s, vendor_id=%s) -- classifier bypassed by source rule.",
            file_path.name,
            classification.vendor_code,
            classification.vendor_id,
        )
        return classification

    if source == DocumentSource.EMAIL:
        if file_path.suffix.lower() in INVOICE_EXTENSIONS:
            logger.info(
                "Forcing document_type=VENDOR_INVOICE for EMAIL PDF '%s' "
                "-- classifier bypassed by source rule.",
                file_path.name,
            )
            return Classification(IncomingDocumentType.VENDOR_INVOICE)
        logger.info(
            "Forcing document_type=CUSTOMER_ORDER for EMAIL spreadsheet '%s' "
            "-- classifier bypassed by source rule.",
            file_path.name,
        )
        return Classification(IncomingDocumentType.CUSTOMER_ORDER)

    return _classify_manual(file_path, metadata, session)


def _classify_manual(
    file_path: Path, metadata: DocumentMetadata, session: Session
) -> Classification:
    """Classifier for MANUAL uploads only. A manual upload always carries an
    explicit `document_type_hint` (the endpoint the user clicked), so the
    heuristic fallbacks below are effectively just defensive."""
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
