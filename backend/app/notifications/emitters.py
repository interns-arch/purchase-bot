"""Helpers that translate integration outcomes into toast notifications, so
the workers stay clean and every publish is failure-isolated (a notification
problem must never affect an import). Import success/failure detail is read
from the `ProcessingResult` the existing `process_document` already returns --
nothing new is computed and no business logic is touched."""

from __future__ import annotations

from backend.app.notifications import broker
from core.logging_setup import get_logger

logger = get_logger(__name__)

_TYPE_LABELS = {
    "VENDOR_INVENTORY": "Vendor Inventory",
    "CUSTOMER_ORDER": "Customer Order",
    "VENDOR_INVOICE": "Vendor Invoice",
    "DELIVERY": "Delivery",
}

_SUCCESS_STATUSES = {"PROCESSED", "PROCESSED_WITH_ERRORS"}
_FAILURE_STATUSES = {"FAILED", "DOWNLOAD_FAILED", "UNSUPPORTED"}


def publish_document_result(source: str, result) -> None:
    """Emit a toast for one processed document. `source` is a display label
    ("WhatsApp" / "Gmail" / "Manual"). Never raises."""
    try:
        doc_type = getattr(getattr(result, "document_type", None), "value", None) or ""
        label = _TYPE_LABELS.get(doc_type, "Document")
        status = getattr(getattr(result, "status", None), "value", None) or ""
        vendor = getattr(result, "vendor_name", None)
        rows = getattr(result, "row_count", 0) or 0

        if status in _SUCCESS_STATUSES:
            lines = []
            if vendor:
                lines.append(f"Vendor: {vendor}")
            lines.append(f"Source: {source}")
            lines.append(
                f"Order Lines: {rows}" if doc_type == "CUSTOMER_ORDER" else f"Records Imported: {rows}"
            )
            broker.publish("success", f"{label} imported successfully.", "\n".join(lines))
        elif status in _FAILURE_STATUSES:
            reason = getattr(result, "message", None) or "Unknown error."
            broker.publish("error", f"{label} import failed.", f"Source: {source}\nReason: {reason}")
        elif status == "NEEDS_REVIEW":
            reason = getattr(result, "message", None) or "Needs manual review."
            broker.publish("warning", f"{label} needs review.", f"Source: {source}\n{reason}")
        elif status == "SKIPPED_DUPLICATE":
            broker.publish(
                "info",
                f"{label} already imported.",
                f"Source: {source}\n(Duplicate file — skipped.)",
            )
    except Exception:  # noqa: BLE001 -- a toast failure must never affect the import
        logger.exception("Failed to publish document-result notification")


def publish_download_failure(source: str, filename: str, reason: str) -> None:
    """Emit a toast when a file couldn't even be downloaded/staged. Never raises."""
    try:
        broker.publish(
            "error",
            "Import failed.",
            f"Source: {source}\nFile: {filename}\nReason: {reason}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to publish download-failure notification")


def publish_sheet_sync(success: bool, vendor_name: str | None, message: str | None = None) -> None:
    """Emit a toast for a Google Sheets sync outcome. Never raises."""
    try:
        if success:
            broker.publish(
                "success",
                "Google Sheet updated successfully.",
                f"Vendor: {vendor_name or '-'}",
            )
        else:
            broker.publish(
                "error",
                "Google Sheet update failed.",
                f"Vendor: {vendor_name or '-'}\nReason: {message or 'Unknown error.'}",
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to publish sheet-sync notification")
