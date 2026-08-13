"""Send the ORIGINAL file straight back to the Founder when an import fails.

A failure notification says WHAT went wrong; this delivers the actual file
so the Founder can open it immediately -- no logging into the web app, no
asking the vendor to re-send. The file is still on disk at this moment
(right after processing), which also makes this the most reliable moment to
capture it: Render's disk is ephemeral, so a file that fails today may be
gone from the server tomorrow -- but it will be sitting in the WhatsApp
chat forever.

Best-effort and failure-isolated, exactly like every other output: the
import outcome is already recorded before this runs, and nothing here can
raise into the caller."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from backend.app.integrations.whatsapp import outbound
from backend.app.integrations.whatsapp.config import whatsapp_settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

# Statuses worth sending the file back for -- the ones where the Founder
# genuinely needs to look at the file itself.
FAILURE_STATUSES = {"FAILED", "NEEDS_REVIEW", "UNSUPPORTED"}

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _caption(result, source: str) -> str:
    doc_type = getattr(getattr(result, "document_type", None), "value", "") or ""
    label = doc_type.replace("_", " ").title() or "Document"
    status = getattr(getattr(result, "status", None), "value", "") or ""
    icon = "⚠️" if status == "NEEDS_REVIEW" else "❌"
    lines = [f"{icon} {label} could not be imported — here is the file that was sent."]
    vendor = getattr(result, "vendor_name", None)
    customer = getattr(result, "customer_name", None)
    sender = getattr(result, "sender", None)
    if vendor:
        lines.append(f"Vendor: {vendor}")
    if customer:
        lines.append(f"Customer: {customer}")
    if sender:
        lines.append(f"Sender: {sender}")
    lines.append(f"Source: {source}")
    reason = getattr(result, "message", None)
    if reason:
        lines.append(f"Reason: {reason}")
    return "\n".join(lines)


def send_failed_file(result, source: str, file_path: Path | str | None) -> bool:
    """Deliver a failed import's original file to every admin number.
    Returns True if at least one send succeeded. Never raises."""
    try:
        if not whatsapp_settings.send_failed_file:
            return False
        status = getattr(getattr(result, "status", None), "value", None)
        if status not in FAILURE_STATUSES:
            return False
        recipients = whatsapp_settings.admin_phone_numbers
        if not recipients or file_path is None:
            return False
        path = Path(file_path)
        if not path.is_file():
            logger.info(
                "Failed file %s is no longer on disk -- notification already sent, "
                "skipping the file send.",
                path.name,
            )
            return False

        content = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or _XLSX_MIME
        caption = _caption(result, source)
        sent = False
        for to in recipients:
            sent = outbound.send_document_safe(to, content, path.name, mime_type, caption) or sent
        if sent:
            logger.info("Sent failed file %s to the Founder for inspection.", path.name)
        return sent
    except Exception:  # noqa: BLE001 -- must never affect the import outcome
        logger.exception("Could not send the failed file to WhatsApp.")
        return False
