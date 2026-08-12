"""Runs after the WhatsApp webhook has already returned its ack response
(scheduled via FastAPI `BackgroundTasks` -- see
`backend/app/api/routes/whatsapp.py`). Opens its own DB session via
`core.db.get_session()` rather than the request-scoped `Depends(get_db)`,
since this genuinely runs after the request that triggered it has already
completed.

Command-routing layer (added for "Customer Orders over WhatsApp"): because
both Vendor Inventory and Customer Order files can now arrive over WhatsApp,
a file is only imported after the sender has first sent a text command
(`Vendor` / `Customer` -- see `commands.py`). The command is remembered
per-number (`command_store`), used to pick which existing import workflow to
run. Non-persistent commands (Vendor, Invoice) are cleared once that one file
has been processed, so the next file requires a fresh command; the
persistent `Customer` command (see `WhatsAppCommand.persistent`) is left
active instead, so multiple Customer Order files sent one after another are
each routed and independently attributed to their own customer without
resending "Customer" before every file -- see
`document_processor.detector._classify_customer_order`. This module only
routes; it never reimplements any import logic -- the Vendor Inventory and
Customer Order imports are reached through the unchanged `process_document`."""

from __future__ import annotations

from pathlib import Path

from backend.app.documents import service as documents_service
from backend.app.documents.models import DocumentSource, IncomingDocumentType
from backend.app.integrations.whatsapp import (
    command_store,
    commands,
    contact_import,
    daily_stock,
    pending_vendor_files,
    registry,
    vendor_memory,
)
from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.commands import WhatsAppCommand
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.media import download_document_media
from backend.app.integrations.whatsapp.outbound import send_reply_safe
from backend.app.integrations.whatsapp import allocation_batch, inventory_output
from backend.app.integrations.whatsapp.parser import (
    IncomingWhatsAppMessage,
    IncomingWhatsAppText,
)
from backend.app.notifications import emitters as notifications
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.processor import process_document
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)


def handle_incoming_whatsapp_text(message: IncomingWhatsAppText) -> None:
    """A plain text message. Priority:
    1. A known routing command -> remember it for this number.
    2. Otherwise, if this number has Vendor Inventory file(s) held while
       waiting for their vendor name -> this text IS the vendor name; import
       every held file for that vendor (identity from the NAME, never the
       filename).
    3. Otherwise -> reply with the instruction (requirement 6)."""
    # Founder command "send reminder" (daily participation follow-up): only
    # honoured from an admin number, checked before anything else so it can
    # never be mistaken for a vendor name.
    if daily_stock.is_reminder_command(message.sender, message.text):
        daily_stock.handle_reminder_command(message.sender)
        return

    # Founder command "register" / "update numbers": the NEXT Excel from this
    # admin number is a vendor contact list that updates the number registry.
    if daily_stock.is_admin_sender(message.sender) and contact_import.is_update_command_text(
        message.text
    ):
        with get_session() as session:
            command_store.set_command(
                message.sender, contact_import.REGISTER_COMMAND_KEY, session
            )
        send_reply_safe(
            message.sender,
            "Send the contact list Excel now — one row per vendor: "
            "Vendor Name + WhatsApp number(s).",
        )
        return

    # A REGISTERED number's texts are never commands or vendor names -- the
    # number itself is the identity, so "good morning sir" etc. is simply
    # ignored (no instruction spam back at a vendor).
    with get_session() as session:
        registered = registry.lookup(message.sender, session)
    if registered is not None:
        logger.info(
            "WhatsApp text from registered %s number %s (%s) ignored: %r",
            registered.party_type,
            message.sender,
            registered.name,
            message.text,
        )
        return

    command = commands.parse_command(message.text)
    if command is None:
        vendor_name = (message.text or "").strip()
        with get_session() as session:
            held = pending_vendor_files.list_for(message.sender, session)
        if held and vendor_name:
            logger.info(
                "WhatsApp text from %s taken as VENDOR NAME %r for %d held file(s).",
                message.sender,
                vendor_name,
                len(held),
            )
            _process_pending_vendor_files(message.sender, vendor_name, held)
            return
        logger.info(
            "WhatsApp text from %s is not a routing command (%r) -- replying with instructions.",
            message.sender,
            message.text,
        )
        send_reply_safe(message.sender, commands.instruction_text())
        return

    with get_session() as session:
        command_store.set_command(message.sender, command.key, session)
    logger.info("WhatsApp routing command from %s stored: %s", message.sender, command.key)
    if command.document_type == IncomingDocumentType.VENDOR_INVENTORY:
        send_reply_safe(
            message.sender,
            f"Got it — now upload your {command.label} file (Excel). "
            "Add the vendor name as the file's caption, or send the vendor "
            "name as a message right after the file.",
        )
    else:
        send_reply_safe(
            message.sender,
            f"Got it — now upload your {command.label} file (Excel).",
        )


def _process_pending_vendor_files(sender: str, vendor_name: str, held) -> None:
    """Import every held Vendor Inventory file for `sender` under the vendor
    name they just supplied, oldest first. Each file is removed from the
    pending store whether its import succeeds or fails (the result toast /
    Import History carries the outcome either way)."""
    send_reply_safe(
        sender,
        f"Importing {len(held)} file(s) for vendor '{vendor_name}'.",
    )
    # Remember the supplied name so further files within the grouping window
    # are grouped under this vendor automatically.
    with get_session() as session:
        vendor_memory.remember(sender, vendor_name, session)
    for row in held:
        file_path = Path(row.staged_path)
        try:
            if not file_path.exists():
                logger.error(
                    "Held vendor file %s for %s no longer exists on disk -- skipping.",
                    row.staged_path,
                    sender,
                )
                notifications.publish_download_failure(
                    "WhatsApp",
                    row.original_filename,
                    "The held file is no longer available -- please re-send it.",
                )
                continue
            metadata = DocumentMetadata(
                sender=sender,
                document_type_hint=IncomingDocumentType.VENDOR_INVENTORY,
                original_filename=row.original_filename,
                vendor_name=vendor_name,
            )
            _process_staged_file(file_path, metadata, row.original_filename)
        except Exception:  # noqa: BLE001 -- one bad held file must not block the rest
            logger.exception(
                "Failed to import held vendor file %s for %s.", row.original_filename, sender
            )
        finally:
            with get_session() as session:
                pending_vendor_files.remove(row.id, session)


def handle_incoming_whatsapp_message(message: IncomingWhatsAppMessage) -> None:
    # Step 1: entering the pipeline.
    logger.info(
        "WhatsApp pipeline step 1: entering handle_incoming_whatsapp_message "
        "(sender=%s, filename=%s, media_id=%s, message_id=%s)",
        message.sender,
        message.filename,
        message.media_id,
        message.message_id,
    )

    # Founder contact-list upload: an admin file captioned "register"/
    # "contacts" (or following a "register" text) UPDATES THE NUMBER REGISTRY
    # instead of importing as stock.
    if daily_stock.is_admin_sender(message.sender):
        if contact_import.is_update_caption(message.caption):
            _handle_contact_update_upload(message)
            return
        with get_session() as session:
            pending_register = contact_import.has_pending_register_command(
                message.sender, whatsapp_settings.grouping_window_minutes, session
            )
        if pending_register:
            _handle_contact_update_upload(message)
            return

    # REGISTERED NUMBER fast path (the permanent identity layer): the
    # sender's number alone identifies the party -- no command, no caption,
    # no filename convention, no grouping window. Commands/captions from
    # registered numbers are deliberately IGNORED so a stray caption can
    # never misfile a registered party's stock.
    with get_session() as session:
        registered = registry.lookup(message.sender, session)
    if registered is not None:
        _handle_registered_upload(message, registered)
        return

    # Routing: which import to run is decided by this sender's last text
    # command. Within the grouping window a previously-used command stays
    # valid (multiple files minutes apart group automatically); an expired
    # one is cleared. No valid command -> do not import; reply with the
    # instruction (requirement 5).
    window = whatsapp_settings.grouping_window_minutes
    with get_session() as session:
        command_key = command_store.get_fresh_command(message.sender, window, session)
    command = commands.get_command(command_key)
    if command is None:
        logger.info(
            "WhatsApp file from %s has no pending routing command -- not importing; "
            "replying with instructions.",
            message.sender,
        )
        send_reply_safe(message.sender, commands.instruction_text())
        return

    logger.info(
        "WhatsApp file from %s routed by command '%s' -> document_type=%s",
        message.sender,
        command.key,
        command.document_type.value,
    )
    try:
        _download_and_process(message, command)
    finally:
        if command.persistent or window > 0:
            # Grouping: the command stays valid for the next file(s) from
            # this sender, and each file RESTARTS the window -- so a batch
            # spread over several minutes routes the same way with no
            # re-asking. Cleared automatically on expiry (get_fresh_command)
            # or when the sender sends a different command.
            with get_session() as session:
                command_store.touch_command(message.sender, session)
            logger.info(
                "Pending WhatsApp command '%s' for %s left active "
                "(grouping window %.0f min restarted).",
                command.key,
                message.sender,
                window,
            )
        else:
            # Legacy behaviour (window disabled): clear the stored command
            # once the file has been processed so the next file requires a
            # fresh command.
            with get_session() as session:
                command_store.clear_command(message.sender, session)
            logger.info("Cleared pending WhatsApp command for %s after processing.", message.sender)


_SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".csv"}


def _handle_contact_update_upload(message: IncomingWhatsAppMessage) -> None:
    """A vendor contact list from an admin number: parse it and update the
    number registry (see `contact_import`), then reply with the summary."""
    logger.info(
        "WhatsApp file '%s' from admin %s taken as a VENDOR CONTACT LIST "
        "(registry update, not a stock import).",
        message.filename,
        message.sender,
    )
    try:
        client = WhatsAppClient(whatsapp_settings)
        file_path = download_document_media(message.media_id, message.filename, client)
    except Exception:
        logger.exception(
            "Could not download the contact list %s from %s.",
            message.media_id,
            message.sender,
        )
        send_reply_safe(
            message.sender, "❌ Could not receive the contact list. Please send it again."
        )
        return

    try:
        rows = contact_import.parse_contact_rows(Path(file_path))
        if not rows:
            send_reply_safe(
                message.sender,
                "⚠️ No vendor rows found in that file. Expected one row per "
                "vendor: Vendor Name + WhatsApp number(s).",
            )
            return
        with get_session() as session:
            reply, stats = contact_import.apply_contact_update(rows, session)
        logger.info("Founder contact update applied: %s", stats)
    except Exception:
        logger.exception("Founder contact update failed for %s.", message.filename)
        send_reply_safe(
            message.sender,
            "❌ Could not read that contact list. Please send an Excel with "
            "Vendor Name and WhatsApp number columns.",
        )
        return
    finally:
        # One list per "register" command -- the next file from this admin is
        # a normal upload again unless they text "register" first.
        with get_session() as session:
            command_store.clear_command(message.sender, session)

    send_reply_safe(message.sender, reply)


def _handle_registered_upload(message: IncomingWhatsAppMessage, party) -> None:
    """A file from a REGISTERED number: identity comes from the registry.
    Vendors: spreadsheet -> Vendor Inventory, PDF -> Vendor Invoice.
    Customers: spreadsheet -> Customer Order (PDFs politely rejected).
    The sender gets a simple result reply; full detail reaches the admin via
    the notification mirror as with every import."""
    suffix = Path(message.filename or "").suffix.lower()
    if party.party_type == "vendor":
        document_type = (
            IncomingDocumentType.VENDOR_INVOICE
            if suffix == ".pdf"
            else IncomingDocumentType.VENDOR_INVENTORY
        )
    else:
        if suffix == ".pdf":
            send_reply_safe(
                message.sender,
                "Please send your order as an Excel file (.xlsx) with Part Number "
                "and Quantity columns.",
            )
            return
        document_type = IncomingDocumentType.CUSTOMER_ORDER

    logger.info(
        "WhatsApp file '%s' from REGISTERED %s number %s -> %s for '%s' "
        "(no command/caption needed; registry is the identity).",
        message.filename,
        party.party_type,
        message.sender,
        document_type.value,
        party.name,
    )

    try:
        client = WhatsAppClient(whatsapp_settings)
        file_path = download_document_media(message.media_id, message.filename, client)
    except Exception:
        logger.exception(
            "WhatsApp pipeline: FAILED downloading media %s from registered number %s",
            message.media_id,
            message.sender,
        )
        with get_session() as session:
            document = documents_service.record_received(
                DocumentSource.WHATSAPP,
                message.filename,
                session,
                sender=message.sender,
                whatsapp_message_id=message.message_id,
            )
            if document.status.value == "RECEIVED":
                documents_service.mark_download_failed(
                    document, "Could not download this attachment from WhatsApp.", session
                )
        notifications.publish_download_failure(
            "WhatsApp", message.filename, "Could not download this attachment from WhatsApp."
        )
        send_reply_safe(
            message.sender,
            "❌ We could not receive this file. Please send it again.",
        )
        return

    metadata = DocumentMetadata(
        sender=message.sender,
        caption=message.caption,  # audit only -- identity comes from the registry
        external_message_id=message.message_id,
        original_filename=message.filename,
        document_type_hint=document_type,
        vendor_id_hint=party.party_id if party.party_type == "vendor" else None,
        customer_id_hint=party.party_id if party.party_type == "customer" else None,
    )
    try:
        result = _process_staged_file(file_path, metadata, message.filename, message.media_id)
    except Exception:
        # process_document reports normal failures via the result status; an
        # exception here is infrastructure-level. The admin already got the
        # error toast/mirror -- the sender still deserves a reply.
        send_reply_safe(
            message.sender,
            "❌ Something went wrong while processing this file. Our team has "
            "been notified -- please try again later.",
        )
        raise
    send_reply_safe(message.sender, _registered_result_reply(party, result, document_type))


def _registered_result_reply(party, result, document_type: IncomingDocumentType) -> str:
    """The short, non-technical reply a registered sender receives. Full
    technical detail (reasons, rejected rows) goes to the admin only."""
    status = getattr(getattr(result, "status", None), "value", None)
    rows = getattr(result, "row_count", 0) or 0
    errors = getattr(result, "error_count", 0) or 0

    if document_type == IncomingDocumentType.VENDOR_INVOICE:
        if status == "PROCESSED":
            return f"✅ Invoice received and verified. {rows} line(s) checked."
        if status == "PROCESSED_WITH_ERRORS":
            return (
                f"⚠️ Invoice received. {rows} line(s) checked, "
                f"{errors} discrepancy(ies) found -- our team will review."
            )
        if status == "SKIPPED_DUPLICATE":
            return "ℹ️ This invoice was already received earlier. Nothing changed."
        if status == "NEEDS_REVIEW":
            return "⚠️ Invoice received -- our team will review it."
        return "❌ We could not read this invoice PDF. Please check the file and resend."

    if document_type == IncomingDocumentType.CUSTOMER_ORDER:
        if status == "PROCESSED":
            return f"✅ Order received successfully. {rows} line(s) imported."
        if status == "PROCESSED_WITH_ERRORS":
            return (
                f"⚠️ Order received. {rows} line(s) imported, {errors} rejected -- "
                "our team will follow up if anything is missing."
            )
        if status == "SKIPPED_DUPLICATE":
            return "ℹ️ This order file was already received earlier. Nothing changed."
        if status == "NEEDS_REVIEW":
            return (
                "⚠️ We received the file but could not find a quantity column. "
                "Please check the file and resend."
            )
        return (
            "❌ We could not read this file. Please send an Excel order with "
            "Part Number and Quantity columns."
        )

    # Vendor Inventory (the everyday case).
    if status == "PROCESSED":
        return f"✅ Stock received successfully. {rows} item(s) imported."
    if status == "PROCESSED_WITH_ERRORS":
        return f"⚠️ Stock received. {rows} item(s) imported, {errors} row(s) skipped."
    if status == "SKIPPED_DUPLICATE":
        return "ℹ️ This stock file was already received earlier. Nothing changed."
    if status == "NEEDS_REVIEW":
        return "⚠️ File received -- our team will review it."
    return (
        "❌ We could not read this file. Please send an Excel file containing "
        "Part Number and Quantity columns."
    )


def _download_and_process(message: IncomingWhatsAppMessage, command: WhatsAppCommand) -> None:
    """Download the media and run it through the existing import workflow the
    resolved `command` maps to. Unchanged from the original single-workflow
    path except that the command-derived `document_type` is passed as a hint
    (honoured by `detector.classify` for WhatsApp)."""
    try:
        client = WhatsAppClient(whatsapp_settings)
        file_path = download_document_media(message.media_id, message.filename, client)
    except Exception:
        # Step 11: any download failure, with full traceback.
        logger.exception(
            "WhatsApp pipeline: FAILED downloading media %s from %s",
            message.media_id,
            message.sender,
        )
        with get_session() as session:
            document = documents_service.record_received(
                DocumentSource.WHATSAPP,
                message.filename,
                session,
                sender=message.sender,
                whatsapp_message_id=message.message_id,
            )
            if document.status.value == "RECEIVED":
                documents_service.mark_download_failed(
                    document, "Could not download this attachment from WhatsApp.", session
                )
        notifications.publish_download_failure(
            "WhatsApp", message.filename, "Could not download this attachment from WhatsApp."
        )
        return

    # Vendor identity for Vendor Inventory comes from the vendor NAME the
    # sender supplies -- the file caption, or a follow-up text message. The
    # filename is audit metadata only. No caption -> first try the grouping
    # window (the name this sender supplied minutes ago groups this file
    # under the SAME vendor automatically); only with no fresh memory is the
    # file held and the sender asked.
    vendor_name = (message.caption or "").strip()
    if command.document_type == IncomingDocumentType.VENDOR_INVENTORY:
        window = whatsapp_settings.grouping_window_minutes
        if not vendor_name:
            with get_session() as session:
                remembered = vendor_memory.recall(message.sender, window, session)
            if remembered:
                vendor_name = remembered
                logger.info(
                    "WhatsApp vendor file '%s' from %s has no caption -- grouped "
                    "under vendor %r supplied within the last %.0f min.",
                    message.filename,
                    message.sender,
                    remembered,
                    window,
                )
        if vendor_name:
            # Every file (captioned OR grouped) RESTARTS the window, exactly
            # like each file restarts the command window.
            with get_session() as session:
                vendor_memory.remember(message.sender, vendor_name, session)
        if not vendor_name:
            with get_session() as session:
                pending_vendor_files.add(message.sender, str(file_path), message.filename, session)
            logger.info(
                "WhatsApp vendor file '%s' from %s staged WITHOUT a vendor name -- "
                "held at %s; asking the sender for the vendor name.",
                message.filename,
                message.sender,
                file_path,
            )
            send_reply_safe(
                message.sender,
                f"Got '{message.filename}'. Which vendor is this inventory from? "
                "Reply with the vendor name (e.g. MAHINDRA).",
            )
            return

    metadata = DocumentMetadata(
        sender=message.sender,
        caption=message.caption,
        external_message_id=message.message_id,
        original_filename=message.filename,
        document_type_hint=command.document_type,
        vendor_name=vendor_name or None,
    )
    _process_staged_file(file_path, metadata, message.filename, message.media_id)


def _process_staged_file(
    file_path, metadata: DocumentMetadata, display_name: str, media_id: str | None = None
) -> None:
    """Run one already-staged WhatsApp file through the unchanged import
    pipeline, then publish the result + trigger the post-commit outputs."""
    logger.info(
        "WhatsApp pipeline: media staged at %s -- opening DB session "
        "(note: get_session() runs Base.metadata.create_all against the configured "
        "database) and starting document processing...",
        file_path,
    )
    result = None
    try:
        with get_session() as session:
            logger.info("WhatsApp pipeline: DB session opened; calling process_document...")
            result = process_document(DocumentSource.WHATSAPP, file_path, metadata, session)
            logger.info(
                "WhatsApp pipeline: process_document finished for %s "
                "(document_id=%s, status=%s, type=%s)",
                display_name,
                getattr(result, "document_id", None),
                getattr(getattr(result, "status", None), "value", None),
                getattr(getattr(result, "document_type", None), "value", None),
            )
    except Exception:
        # Step 11: any processing/DB failure, with full traceback. Re-raised so
        # behaviour is unchanged -- only observability is added. The caller's
        # `finally` still clears the pending command (requirement 4).
        logger.exception(
            "WhatsApp pipeline: FAILED during DB session / process_document for %s (media_id=%s)",
            display_name,
            media_id,
        )
        raise

    # The session context above has now COMMITTED -- only from this point may a
    # success be announced. Publishing inside the session block would toast
    # SUCCESS for a transaction that could still fail at commit.
    if result is not None:
        notifications.publish_document_result("WhatsApp", result)

    # Temporary Google-Sheets replacement (output layer): the import above is
    # now committed, so on a SUCCESSFUL Vendor Inventory import request the
    # consolidated all-vendor workbook for the Founder over WhatsApp. The
    # request is DEBOUNCED: a batch of vendor files produces ONE final
    # workbook (sent after the batch goes quiet), not one per file. Fully
    # best-effort -- it opens its own session and cannot affect the import.
    if _is_successful_inventory_import(result):
        inventory_output.request_consolidated_send(getattr(result, "vendor_name", None))

    # Founder automation ("Combined ZIP" mode): a successfully imported
    # customer order is queued for automatic vendor selection; the batch runs
    # after order imports go quiet and sends ONE ZIP of allocation reports.
    order_id = _successful_customer_order_id(result)
    if order_id is not None:
        allocation_batch.request_order_allocation(order_id)

    return result


def _is_successful_inventory_import(result) -> bool:
    doc_type = getattr(getattr(result, "document_type", None), "value", None)
    status = getattr(getattr(result, "status", None), "value", None)
    return doc_type == "VENDOR_INVENTORY" and status in ("PROCESSED", "PROCESSED_WITH_ERRORS")


def _successful_customer_order_id(result) -> int | None:
    doc_type = getattr(getattr(result, "document_type", None), "value", None)
    status = getattr(getattr(result, "status", None), "value", None)
    if doc_type == "CUSTOMER_ORDER" and status in ("PROCESSED", "PROCESSED_WITH_ERRORS"):
        return getattr(result, "customer_order_id", None)
    return None
