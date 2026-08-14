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

        customer = getattr(result, "customer_name", None)
        sender = getattr(result, "sender", None)

        if status in _SUCCESS_STATUSES:
            errors = getattr(result, "error_count", 0) or 0
            is_invoice = doc_type == "VENDOR_INVOICE"
            lines = []
            if vendor:
                lines.append(f"Vendor: {vendor}")
            if customer:
                lines.append(f"Customer: {customer}")
            lines.append(f"Source: {source}")
            if is_invoice:
                # An invoice is VERIFIED against allocations, not imported as
                # stock -- say what the numbers actually mean.
                lines.append(f"Lines Matched: {rows}")
            elif doc_type == "CUSTOMER_ORDER":
                lines.append(f"Order Lines: {rows}")
            else:
                lines.append(f"Records Imported: {rows}")
            if status == "PROCESSED_WITH_ERRORS" or errors:
                # Partial result: never report a plain success -- expose the
                # real split and the reason when one exists.
                lines.append(f"Discrepancies: {errors}" if is_invoice else f"Rows Rejected: {errors}")
                reason = getattr(result, "message", None)
                if reason:
                    lines.append(f"Reason: {reason}")
                lines.append(
                    "See the Vendor Invoices page for line-level verification."
                    if is_invoice
                    else "See Import History for row-level details."
                )
                if getattr(result, "sheet_synced", False):
                    lines.append("Google Sheet: updated")
                broker.publish(
                    "warning",
                    f"{label} verified with discrepancies." if is_invoice
                    else f"{label} imported with errors.",
                    "\n".join(lines),
                )
            else:
                # A clean success may still carry a note worth showing -- e.g.
                # "New vendor onboarded with code X" or the AI-assisted
                # column-mapping provenance.
                note = getattr(result, "message", None)
                if note:
                    lines.append(str(note))
                if getattr(result, "sheet_synced", False):
                    # Folded in rather than sent as its own message.
                    lines.append("Google Sheet: updated")
                broker.publish("success", f"{label} imported successfully.", "\n".join(lines))
        elif status in _FAILURE_STATUSES:
            # WHOSE file failed: vendor/customer when known (WhatsApp caption
            # or number registry resolves them before the import runs), and
            # always the sender's number/address so no failure is anonymous.
            reason = getattr(result, "message", None) or "Unknown error."
            lines = []
            if vendor:
                lines.append(f"Vendor: {vendor}")
            if customer:
                lines.append(f"Customer: {customer}")
            if sender:
                lines.append(f"Sender: {sender}")
            file_name = getattr(result, "file_name", None)
            if file_name:
                lines.append(f"File: {file_name}")
            lines.append(f"Source: {source}")
            lines.append(f"Reason: {reason}")
            broker.publish("error", f"{label} import failed.", "\n".join(lines))
        elif status == "NEEDS_REVIEW":
            reason = getattr(result, "message", None) or "Needs manual review."
            lines = []
            if vendor:
                lines.append(f"Vendor: {vendor}")
            if customer:
                lines.append(f"Customer: {customer}")
            if sender:
                lines.append(f"Sender: {sender}")
            lines.append(f"Source: {source}")
            lines.append(reason)
            broker.publish("warning", f"{label} needs review.", "\n".join(lines))
        elif status == "SKIPPED_DUPLICATE":
            lines = []
            if vendor:
                lines.append(f"Vendor: {vendor}")
            if customer:
                lines.append(f"Customer: {customer}")
            lines.append(f"Source: {source}")
            lines.append("(Duplicate file — skipped.)")
            broker.publish("info", f"{label} already imported.", "\n".join(lines))
    except Exception:  # noqa: BLE001 -- a toast failure must never affect the import
        logger.exception("Failed to publish document-result notification")


def publish_topup(result) -> None:
    """Emit a toast when newly-imported vendor stock filled shortfalls on
    existing customer orders (auto top-up). Silent when nothing changed --
    the common case, and the Founder should not get a message per import.
    Never raises."""
    try:
        if not getattr(result, "lines", None):
            return
        vendor = getattr(result, "vendor_name", None) or "the new stock"
        detail = "\n".join(result.summary_lines())
        # WEB-ONLY: this part-by-part list is long and hard to read on a
        # phone. WhatsApp receives the reallocation WORKBOOK instead
        # (`integrations/whatsapp/topup_output.py`) -- same format as the
        # allocation report, one worksheet per order.
        broker.publish(
            "success",
            f"New stock from {vendor} filled {len(result.order_ids)} pending order(s).",
            f"{detail}\n\nExisting allocations were not changed.",
            mirror=False,
        )
    except Exception:  # noqa: BLE001 -- a toast failure must never affect the import
        logger.exception("Failed to publish top-up notification")


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
            # Web-only: a SUCCESSFUL sync is already reported as the
            # "Google Sheet: updated" line inside that vendor's single import
            # message, so a separate WhatsApp message would just repeat it.
            # A FAILED sync still goes to WhatsApp -- that one needs action.
            broker.publish(
                "success",
                "Google Sheet updated successfully.",
                f"Vendor: {vendor_name or '-'}",
                mirror=False,
            )
        else:
            broker.publish(
                "error",
                "Google Sheet update failed.",
                f"Vendor: {vendor_name or '-'}\nReason: {message or 'Unknown error.'}",
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to publish sheet-sync notification")
