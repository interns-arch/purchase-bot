"""Classifies an incoming file into an `IncomingDocumentType`.

The channel a document arrived on is authoritative (business workflow), so
`classify()` routes by `source` first and only MANUAL uploads reach the
heuristic classifier:

- WHATSAPP -> decided by the sender's last routing command (Vendor /
  Customer / Invoice -- see `backend.app.integrations.whatsapp.commands`),
  passed in as `metadata.document_type_hint`. Vendor Inventory resolves the
  vendor from the filename's Vendor Code (see `_classify_inventory`,
  AR_CT.xlsx / BI_CT.xlsx); Customer Order resolves the customer from the
  filename's Customer Code the same way (see `_classify_customer_order`,
  AB_CO_Order.xlsx). Either way an unregistered code is rejected downstream
  rather than silently misrouted. With no hint, defaults to Vendor Inventory
  (backward-compatible with any direct caller).
- EMAIL (dedicated Gmail inbox) -> Customer Order for spreadsheets, Vendor
  Invoice for PDF purchase bills -- decided purely by file format. Gmail
  never identifies a customer (no Customer Code in an email attachment's
  name to parse), so its Customer Orders always get `customer_id=None`,
  exactly as before Customer Codes existed.
- MANUAL -> `_classify_manual`: the explicit `document_type_hint` from the
  endpoint the user clicked (a manual Vendor Inventory upload's *vendor* is
  still resolved from its filename's Vendor Code, exactly like WhatsApp).

Vendor Inventory and Customer Order files are NOT distinguishable by column
headers alone (both only require Part Number + Quantity per
`core.ingestion.column_detector.find_required_columns`) -- which is why
source, not file structure, is the primary signal.

Sender identity: a number REGISTERED in the WhatsApp number registry
(`integrations.whatsapp.registry`) is authoritative -- the worker passes the
registered party in as `vendor_id_hint` / `customer_id_hint` and the fast
paths below use it directly. UNREGISTERED senders share the one WhatsApp
Business number-agnostic flow: their phone number can never tell parties
apart, so identity still comes from the supplied name / Customer Code
exactly as before the registry existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.documents.models import DocumentSource, IncomingDocumentType
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.validator import INVOICE_EXTENSIONS
from core.logging_setup import get_logger
from core.services import customer_code_service, vendor_code_service

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
    customer_id: int | None = None
    # Same "code present but not registered" signal as `vendor_code`, but for
    # a Customer Order's Customer Code (e.g. "AB_CO").
    customer_code: str | None = None
    # True only when `_classify_customer_order` actually ran (WhatsApp
    # Customer Order files) -- distinguishes "no code-shaped prefix, but
    # customer identification was attempted" (dispatcher should onboard a new
    # customer by filename) from Gmail/manual Customer Orders, which never
    # attempt customer identification at all and must keep `customer_id`
    # unset exactly as before this feature existed.
    resolve_customer: bool = False
    # The vendor NAME the sender supplied for a WhatsApp Vendor Inventory
    # file (caption or follow-up text). The dispatcher resolves it against
    # the existing Vendor master (`_resolve_or_onboard_vendor`).
    vendor_name: str | None = None
    # True only for the WhatsApp Vendor Inventory flow: vendor identity MUST
    # come from `vendor_name` -- if it is missing the dispatcher fails with a
    # clear message instead of guessing from the filename. Manual uploads
    # keep their pre-existing filename behaviour (flag stays False).
    require_vendor_name: bool = False


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


def _classify_customer_order(file_path: Path, session: Session) -> Classification:
    """Resolves the customer for a WhatsApp Customer Order file from its
    filename's Customer Code, mirroring `_classify_inventory` exactly.
    `resolve_customer=True` is set regardless of whether a code was found, so
    the dispatcher knows customer identification was actually attempted (as
    opposed to Gmail/manual Customer Orders, which never call this)."""
    code = customer_code_service.parse_customer_code_from_filename(file_path.name)
    if code is None:
        return Classification(IncomingDocumentType.CUSTOMER_ORDER, resolve_customer=True)

    customer = customer_code_service.get_customer_by_code(code, session)
    return Classification(
        IncomingDocumentType.CUSTOMER_ORDER,
        customer_id=customer.id if customer is not None else None,
        customer_code=code,
        resolve_customer=True,
    )


def classify(
    file_path: Path, metadata: DocumentMetadata, session: Session, *, source: DocumentSource
) -> Classification:
    """Decide a document's type. The channel it arrived on is authoritative
    per the business workflow, so WhatsApp and Gmail bypass the heuristic
    classifier entirely -- only MANUAL uploads are classified:

    - WHATSAPP -> the type is decided by the sender's routing command
      (Vendor / Customer / ...), which the WhatsApp worker passes in as
      `document_type_hint`. Vendor Inventory identity comes from the vendor
      NAME the sender supplied (`metadata.vendor_name` -- caption or
      follow-up text), never from the filename. With no hint it defaults to
      Vendor Inventory (backward-compatible with any direct caller).
    - EMAIL (dedicated Gmail inbox) -> Customer Order for spreadsheets, or
      Vendor Invoice for PDF purchase bills, decided purely by file format.
    - MANUAL -> the hint/heuristic classifier below (`_classify_manual`)."""
    if source == DocumentSource.WHATSAPP:
        hint = metadata.document_type_hint
        if hint is None or hint == IncomingDocumentType.VENDOR_INVENTORY:
            # REGISTERED NUMBER fast path (number registry): the sender's
            # WhatsApp number already identifies the vendor -- pass the id
            # straight through. No name resolution, no caption, no filename.
            if metadata.vendor_id_hint is not None:
                logger.info(
                    "WHATSAPP document '%s' routed to VENDOR_INVENTORY by the "
                    "sender's registered number (vendor_id=%s).",
                    file_path.name,
                    metadata.vendor_id_hint,
                )
                return Classification(
                    IncomingDocumentType.VENDOR_INVENTORY,
                    vendor_id=metadata.vendor_id_hint,
                )
            # Vendor Inventory: identity comes from the vendor NAME the
            # sender supplied (caption or follow-up text), resolved by the
            # dispatcher against the existing Vendor master. The filename is
            # audit metadata only -- it is NEVER used to identify the vendor
            # (the same vendor may send stock.xlsx, August_Final.xlsx, ...).
            classification = Classification(
                IncomingDocumentType.VENDOR_INVENTORY,
                vendor_name=(metadata.vendor_name or "").strip() or None,
                require_vendor_name=True,
            )
            logger.info(
                "WHATSAPP document '%s' routed to VENDOR_INVENTORY "
                "(vendor_name=%s; filename is metadata only).",
                file_path.name,
                classification.vendor_name,
            )
            return classification
        if hint == IncomingDocumentType.CUSTOMER_ORDER:
            # REGISTERED NUMBER fast path: the sender's number already
            # identifies the customer -- mirrors the vendor fast path above.
            if metadata.customer_id_hint is not None:
                logger.info(
                    "WHATSAPP document '%s' routed to CUSTOMER_ORDER by the "
                    "sender's registered number (customer_id=%s).",
                    file_path.name,
                    metadata.customer_id_hint,
                )
                return Classification(
                    IncomingDocumentType.CUSTOMER_ORDER,
                    customer_id=metadata.customer_id_hint,
                    resolve_customer=True,
                )
            # Customer Order (see `_classify_customer_order`): resolve which
            # customer this file belongs to from its filename's Customer
            # Code, exactly as Vendor Inventory resolves its vendor above --
            # each file arriving under a persisted "Customer" command is
            # classified independently, so consecutive files never get merged
            # into one customer.
            classification = _classify_customer_order(file_path, session)
            logger.info(
                "WHATSAPP document '%s' routed to CUSTOMER_ORDER "
                "(customer_code=%s, customer_id=%s).",
                file_path.name,
                classification.customer_code,
                classification.customer_id,
            )
            return classification

        # Any other command-routed type (INVOICE today) -- import logic is
        # reached unchanged via the dispatcher.
        logger.info(
            "WHATSAPP document '%s' routed to %s by the sender's command.",
            file_path.name,
            hint.value,
        )
        return Classification(hint)

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
