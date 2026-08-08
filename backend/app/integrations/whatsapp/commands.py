"""WhatsApp routing commands.

Because both Vendor Inventory and Customer Order files can now arrive over
WhatsApp, the user first sends a text command telling the system which
import to run for their next file. This module is the single, extensible
registry mapping a command word to the `IncomingDocumentType` its file
should be imported as.

To support a new command in future (e.g. `Invoice`, `Purchase Order`) add
ONE entry to `_COMMANDS` -- nothing else in the routing layer, and none of
the existing import workflows, needs to change. The instruction message and
per-command reply are generated from this registry, so they stay in sync
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.documents.models import IncomingDocumentType


@dataclass(frozen=True)
class WhatsAppCommand:
    key: str  # canonical, lowercase -- what we persist (e.g. "vendor")
    label: str  # user-facing display (e.g. "Vendor")
    document_type: IncomingDocumentType  # which existing import workflow to run


# The extension point. Order here is the order shown in the instruction text.
_COMMANDS: dict[str, WhatsAppCommand] = {
    command.key: command
    for command in (
        WhatsAppCommand("vendor", "Vendor", IncomingDocumentType.VENDOR_INVENTORY),
        WhatsAppCommand("customer", "Customer", IncomingDocumentType.CUSTOMER_ORDER),
        # Routes the next (PDF) file to the existing Vendor Invoice importer
        # (detector.classify honours this hint for WhatsApp; dispatcher sends it
        # to vendor_invoice_verification_service). "invoice"/"Invoice"/"INVOICE"
        # all match -- parse_command lowercases the incoming text.
        WhatsAppCommand("invoice", "Invoice", IncomingDocumentType.VENDOR_INVOICE),
    )
}


def parse_command(text: str | None) -> WhatsAppCommand | None:
    """Resolve a raw inbound text message to a known command, or None if it
    isn't one. Match is case-insensitive and whitespace-tolerant, but the
    whole message must be exactly a command word (so ordinary chatter is
    never mistaken for a command)."""
    if not text:
        return None
    return _COMMANDS.get(text.strip().lower())


def get_command(key: str | None) -> WhatsAppCommand | None:
    """Look up a previously-stored canonical command key."""
    if not key:
        return None
    return _COMMANDS.get(key)


def instruction_text() -> str:
    """The reply sent when a file arrives with no pending command, or when an
    unrecognised text is received. Generated from the registry so new
    commands appear automatically."""
    options = "\n".join(command.label for command in _COMMANDS.values())
    return "Please send one of the following before uploading a file:\n\n" + options
