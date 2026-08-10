"""One-click purge of file-derived data, scoped by document family.

Deletes TRANSACTIONAL data only -- everything that came from uploaded files.
It never touches master/config data: Vendor and Customer records (and their
permanent codes), users/auth, WhatsApp/Gmail/Sheets integration settings and
status rows all survive every scope, so the system keeps working normally
with an empty history.

Scopes (children always deleted before parents so the plain DELETEs are
FK-safe on both Postgres and SQLite):

- vendor:   vendor inventory imports (rows, row errors, imports), held
            WhatsApp vendor files, and their Document Inbox records.
- customer: customer orders (items, row errors, vendor selections /
            reservations, generated vendor POs) and their Inbox records.
- invoice:  vendor invoice imports (line results) and their Inbox records.
- all:      the three scopes above PLUS delivery-tracking data, the legacy
            CLI PO/delivery chain, and the Part/PartAlias master built up
            from imports -- a full transactional reset.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.app.documents.models import IncomingDocument, IncomingDocumentType
from backend.app.integrations.whatsapp.models import WhatsAppPendingVendorFile
from core.logging_setup import get_logger
from core.models import (
    CustomerOrder,
    CustomerOrderImportError,
    CustomerOrderItem,
    DeliveryImport,
    DeliveryImportError,
    DeliveryItem,
    ImportErrorRecord,
    InventoryImport,
    Part,
    PartAlias,
    PurchaseOrder,
    PurchaseOrderItem,
    VendorDeliveryImport,
    VendorDeliveryImportError,
    VendorDeliveryItem,
    VendorInventory,
    VendorInvoiceImport,
    VendorInvoiceLineResult,
    VendorPurchaseOrder,
    VendorPurchaseOrderItem,
    VendorSelection,
)

logger = get_logger(__name__)

PURGE_SCOPES = ("all", "vendor", "customer", "invoice")


def _wipe(session: Session, model, statement=None) -> tuple[str, int]:
    result = session.execute(statement if statement is not None else delete(model))
    return model.__tablename__, result.rowcount or 0


def _purge_vendor(session: Session) -> list[tuple[str, int]]:
    return [
        _wipe(session, VendorInventory),
        _wipe(session, ImportErrorRecord),
        _wipe(session, InventoryImport),
        _wipe(session, WhatsAppPendingVendorFile),
        _wipe(
            session,
            IncomingDocument,
            delete(IncomingDocument).where(
                IncomingDocument.document_type == IncomingDocumentType.VENDOR_INVENTORY
            ),
        ),
    ]


def _purge_customer(session: Session) -> list[tuple[str, int]]:
    return [
        _wipe(session, VendorPurchaseOrderItem),
        _wipe(session, VendorPurchaseOrder),
        _wipe(session, VendorSelection),
        _wipe(session, CustomerOrderImportError),
        _wipe(session, CustomerOrderItem),
        _wipe(session, CustomerOrder),
        _wipe(
            session,
            IncomingDocument,
            delete(IncomingDocument).where(
                IncomingDocument.document_type == IncomingDocumentType.CUSTOMER_ORDER
            ),
        ),
    ]


def _purge_invoice(session: Session) -> list[tuple[str, int]]:
    return [
        _wipe(session, VendorInvoiceLineResult),
        _wipe(session, VendorInvoiceImport),
        _wipe(
            session,
            IncomingDocument,
            delete(IncomingDocument).where(
                IncomingDocument.document_type == IncomingDocumentType.VENDOR_INVOICE
            ),
        ),
    ]


def purge_files(scope: str, session: Session) -> dict[str, int]:
    """Delete all file-derived data for `scope`. Returns {table: rows_deleted}
    (zero-count tables omitted). Raises ValueError on an unknown scope; the
    caller owns the transaction (commit/rollback)."""
    if scope not in PURGE_SCOPES:
        raise ValueError(f"Unknown purge scope {scope!r}. Valid: {', '.join(PURGE_SCOPES)}.")

    deleted: list[tuple[str, int]] = []
    if scope == "vendor":
        deleted += _purge_vendor(session)
    elif scope == "customer":
        deleted += _purge_customer(session)
    elif scope == "invoice":
        deleted += _purge_invoice(session)
    else:  # all -- children before parents across every family
        deleted += _purge_invoice(session)
        # delivery tracking (web app) + legacy CLI delivery/PO chain
        deleted.append(_wipe(session, VendorDeliveryItem))
        deleted.append(_wipe(session, VendorDeliveryImportError))
        deleted.append(_wipe(session, VendorDeliveryImport))
        deleted.append(_wipe(session, DeliveryItem))
        deleted.append(_wipe(session, DeliveryImportError))
        deleted.append(_wipe(session, DeliveryImport))
        deleted += _purge_customer(session)
        deleted.append(_wipe(session, PurchaseOrderItem))
        deleted.append(_wipe(session, PurchaseOrder))
        deleted += _purge_vendor(session)
        # part master built from imports -- every RESTRICT holder is gone now
        deleted.append(_wipe(session, PartAlias))
        deleted.append(_wipe(session, Part))
        deleted.append(_wipe(session, IncomingDocument))  # any remaining types

    session.flush()
    counts = {table: n for table, n in deleted if n}
    logger.info("Data purge scope=%s deleted: %s", scope, counts or "nothing (already empty)")
    return counts
