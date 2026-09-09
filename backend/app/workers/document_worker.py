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
from backend.app.documents.models import (
    DocumentSource,
    IncomingDocument,
    IncomingDocumentType,
)
from backend.app.integrations import dealer_portal
from backend.app.integrations.whatsapp import (
    command_store,
    commands,
    contact_import,
    customer_text_order,
    daily_stock,
    failed_file,
    pending_customer_files,
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
from backend.app.services.document_processor import staging
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.processor import process_document
from backend.app.services.topup_runner import run_topup_for_vendor
from core.db import get_session
from core.services import customer_code_service
from core.logging_setup import get_logger
from core.time_utils import now_ist

logger = get_logger(__name__)


def handle_incoming_whatsapp_text(message: IncomingWhatsAppText) -> None:
    """Entry point for a plain text message -- a LAST-RESORT wrapper.

    This runs as a FastAPI BackgroundTask, so an exception escaping the real
    handler is swallowed by the task machinery: the sender gets total silence
    and the log says nothing useful. Every branch below already guards
    itself; this exists so that a bug nobody anticipated still leaves a
    traceback and, for an ordinary sender, some acknowledgement."""
    try:
        _handle_incoming_whatsapp_text(message)
    except Exception:  # noqa: BLE001 -- silence is the worst possible outcome
        logger.exception(
            "Unhandled error while processing the WhatsApp text from %s: %r",
            message.sender,
            (message.text or "")[:120],
        )
        # Deliberately NOT sent to a registered vendor/customer -- their texts
        # are meant to be ignored, and an error reply would be exactly the
        # instruction spam that rule exists to prevent.
        try:
            with get_session() as session:
                known = registry.lookup(message.sender, session)
            if known is None:
                send_reply_safe(
                    message.sender,
                    "Sorry — something went wrong handling that message. "
                    "Please try again.",
                )
        except Exception:  # noqa: BLE001
            logger.exception("Could not send the fallback reply to %s.", message.sender)


def _handle_incoming_whatsapp_text(message: IncomingWhatsAppText) -> None:
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

    # Admin answering "where does this vendor's stock go on Dealer Portal?"
    # ("dp 1", "dp 1367", "dp skip"). Asked once per vendor and remembered,
    # so this is a short-lived reply, checked before the register commands.
    if daily_stock.is_admin_sender(message.sender):
        try:
            with get_session() as session:
                handled, reply = dealer_portal.resolve_mapping_reply(
                    message.text, session
                )
            if handled:
                send_reply_safe(message.sender, reply)
                return
        except Exception:  # noqa: BLE001 -- never block the other commands
            logger.exception("Could not apply the Dealer Portal mapping reply.")

    # Founder command "register customer": the NEXT Excel from this admin
    # number is a CUSTOMER contact list. The same message may instead carry
    # the customers inline ("Karol Bagh 9812345678" one per line), in which
    # case it is applied immediately without waiting for a file.
    if daily_stock.is_admin_sender(message.sender) and contact_import.is_customer_command_text(
        message.text
    ):
        with get_session() as session:
            command_store.set_command(
                message.sender, contact_import.CUSTOMER_COMMAND_KEY, session
            )
        send_reply_safe(
            message.sender,
            "Send the customer list now — either an Excel (one row per "
            "customer: Customer Name + WhatsApp number(s)), or just type them "
            "here, one per line:\n\n"
            "Karol Bagh 9812345678\n"
            "Rohini Auto 9811122233",
        )
        return

    # The customer list typed as a MESSAGE rather than sent as a file --
    # accepted whenever an admin's text parses as name+number pairs, so the
    # Founder can register without preparing a sheet.
    if daily_stock.is_admin_sender(message.sender):
        with get_session() as session:
            awaiting_customers = contact_import.has_pending_customer_command(
                message.sender, whatsapp_settings.grouping_window_minutes, session
            )
        if awaiting_customers:
            rows = contact_import.parse_contact_text(message.text)
            if rows:
                _apply_customer_contacts_safe(message.sender, rows)
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
            "Vendor Name + WhatsApp number(s). Only the vendors in the "
            "sheet are updated — every other vendor stays as it is.",
        )
        return

    # Founder command "remove team": the NEXT Excel lists members to REMOVE
    # from the purchase team (checked before "register team" -- "remove team"
    # must never be mistaken for an update).
    if daily_stock.is_admin_sender(message.sender) and contact_import.is_remove_team_command_text(
        message.text
    ):
        with get_session() as session:
            command_store.set_command(
                message.sender, contact_import.REMOVE_TEAM_COMMAND_KEY, session
            )
        send_reply_safe(
            message.sender,
            "Send the Excel of members to REMOVE from the purchase team — "
            "one row per member (Name, or Name + Number). Everyone else stays.",
        )
        return

    # Founder command "register team": the NEXT Excel is the purchase-team
    # list (Name + WhatsApp number) -- these members receive every PO.
    if daily_stock.is_admin_sender(message.sender) and contact_import.is_team_command_text(
        message.text
    ):
        with get_session() as session:
            command_store.set_command(
                message.sender, contact_import.TEAM_COMMAND_KEY, session
            )
        send_reply_safe(
            message.sender,
            "Send the purchase-team Excel now — one row per member: "
            "Name + WhatsApp number. New members are ADDED and listed ones "
            "updated; members not in the sheet are kept. "
            'To remove someone, text "remove team" instead.',
        )
        return

    # A REGISTERED number's texts are never commands or vendor names -- the
    # number itself is the identity, so "good morning sir" etc. is simply
    # ignored (no instruction spam back at a vendor).
    registered = None
    if whatsapp_settings.registry_enabled:
        with get_session() as session:
            registered = registry.lookup(message.sender, session)
    if registered is not None:
        # A registered CUSTOMER may simply type their part numbers -- the
        # number is the identity, so no command and no file are needed. Any
        # other text from a registered number stays ignored exactly as
        # before (no instruction spam back at a vendor).
        if registered.party_type == "customer":
            order = customer_text_order.parse_text_order(message.text)
            if order.is_order:
                logger.info(
                    "WhatsApp text from registered customer %s (%s) read as an "
                    "ORDER of %d part(s).",
                    message.sender,
                    registered.name,
                    len(order.lines),
                )
                _process_text_order(message.sender, order, customer_id=registered.party_id)
                return
        logger.info(
            "WhatsApp text from registered %s number %s (%s) ignored: %r",
            registered.party_type,
            message.sender,
            registered.name,
            message.text,
        )
        return

    # A TYPED customer order from an unregistered sender: they texted
    # "customer" (the command persists), then sent the part numbers as an
    # ordinary message instead of a file. Excel is unaffected -- this only
    # fires when the text actually parses as part numbers, so a vendor name
    # or ordinary chatter still falls through to the handling below.
    with get_session() as session:
        pending = command_store.get_fresh_command(
            message.sender, whatsapp_settings.grouping_window_minutes, session
        )
    if pending == "customer":
        order = customer_text_order.parse_text_order(message.text)
        if order.is_order:
            logger.info(
                "WhatsApp text from %s read as a TYPED customer order: %d part(s), "
                "customer=%r.",
                message.sender,
                len(order.lines),
                order.customer_name,
            )
            _process_text_order(message.sender, order)
            return

    command = commands.parse_command(message.text)
    if command is None:
        supplied_name = (message.text or "").strip()
        # Customer Order files held while waiting for their customer name --
        # checked before vendor files so the answer goes to whichever kind of
        # file this sender actually has waiting.
        held_customer = []
        try:
            with get_session() as session:
                held_customer = pending_customer_files.list_for(message.sender, session)
        except Exception:  # noqa: BLE001 -- new table; never silence the reply
            logger.exception(
                "Held-customer-file lookup failed for %s -- continuing.",
                message.sender,
            )
        if held_customer and supplied_name:
            logger.info(
                "WhatsApp text from %s taken as CUSTOMER NAME %r for %d held file(s).",
                message.sender,
                supplied_name,
                len(held_customer),
            )
            _process_pending_customer_files(message.sender, supplied_name, held_customer)
            return

        vendor_name = supplied_name
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


def _apply_customer_contacts_safe(sender: str, rows) -> None:
    """Apply a typed customer list and reply with exactly what changed.
    Never raises -- a registry failure must not kill the worker thread."""
    try:
        with get_session() as session:
            reply, stats = contact_import.apply_customer_contact_update(rows, session)
        logger.info("Customer registry updated from a typed list: %s", stats)
        send_reply_safe(sender, reply)
    except Exception:  # noqa: BLE001
        logger.exception("Could not apply the typed customer contact list.")
        send_reply_safe(
            sender,
            "Sorry — that customer list could not be applied. Please check the "
            "numbers and try again.",
        )


def _process_text_order(
    sender: str, order, customer_id: int | None = None, *, notify: bool = True
) -> None:
    """Turn a TYPED customer order into the CSV the normal importer reads,
    then run it through the unchanged pipeline.

    Nothing downstream knows the difference: `run_customer_order_import`
    already accepts CSV, so allocation and the vendor-wise workbook behave
    exactly as they do for an Excel upload. Never raises."""
    try:
        if notify:
            send_reply_safe(sender, customer_text_order.summary_reply(order))

        content = customer_text_order.build_csv(order)
        # The staged file is kept for audit like any other upload, and the
        # name says where it came from.
        #
        # The name must be UNIQUE PER ORDER. `run_customer_order_import`
        # treats a matching (file_name, content_hash) pair as a duplicate and
        # skips it -- and the customer name is NOT in the CSV (it travels as
        # metadata), so two orders for DIFFERENT customers with the same
        # parts have identical content. Without microseconds and the sender
        # in the name, two confirmations in the same second would collide and
        # the second order would be silently dropped.
        stamp = now_ist().strftime("%Y%m%d_%H%M%S_%f")
        file_name = f"whatsapp_text_order_{stamp}_{(sender or 'x')[-4:]}.csv"
        file_path = staging.save_incoming_bytes(
            content, file_name, DocumentSource.WHATSAPP
        )

        metadata = DocumentMetadata(
            sender=sender,
            caption=None,
            original_filename=file_name,
            document_type_hint=IncomingDocumentType.CUSTOMER_ORDER,
            customer_id_hint=customer_id,
            customer_name=order.customer_name,
        )
        _process_staged_file(file_path, metadata, file_name, notify_sender=False)
    except Exception:  # noqa: BLE001 -- a typed order must never kill the worker
        logger.exception("Could not process the typed customer order from %s.", sender)
        send_reply_safe(
            sender,
            "Sorry — that order could not be processed. Please re-send the part "
            "numbers, or send them as an Excel file.",
        )


def _process_pending_customer_files(sender: str, customer_name: str, held) -> None:
    """Import every held Customer Order file for `sender` under the customer
    name they just supplied, oldest first. The counterpart of
    `_process_pending_vendor_files`; each file is removed from the pending
    store whether its import succeeds or fails."""
    send_reply_safe(
        sender, f"Importing {len(held)} file(s) for customer '{customer_name}'."
    )
    for row in held:
        file_path = Path(row.staged_path)
        try:
            if not file_path.exists():
                logger.error(
                    "Held customer file %s for %s no longer exists on disk -- skipping.",
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
                document_type_hint=IncomingDocumentType.CUSTOMER_ORDER,
                original_filename=row.original_filename,
                customer_name=customer_name,
            )
            _process_staged_file(file_path, metadata, row.original_filename)
        except Exception:  # noqa: BLE001 -- one bad held file must not block the rest
            logger.exception(
                "Failed to import held customer file %s for %s.",
                row.original_filename,
                sender,
            )
        finally:
            with get_session() as session:
                pending_customer_files.remove(row.id, session)


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
    # instead of importing as stock. "register team" works the same way for
    # the purchase-team list.
    if daily_stock.is_admin_sender(message.sender):
        caption_lower = (message.caption or "").strip().lower()
        with get_session() as session:
            pending_register = contact_import.has_pending_register_command(
                message.sender, whatsapp_settings.grouping_window_minutes, session
            )
            pending_team = contact_import.has_pending_team_command(
                message.sender, whatsapp_settings.grouping_window_minutes, session
            )
            pending_remove_team = contact_import.has_pending_remove_team_command(
                message.sender, whatsapp_settings.grouping_window_minutes, session
            )
            pending_customer = contact_import.has_pending_customer_command(
                message.sender, whatsapp_settings.grouping_window_minutes, session
            )
        if contact_import.is_customer_caption(message.caption) or pending_customer:
            _handle_customer_contact_upload(message)
            return
        if caption_lower in contact_import.REMOVE_TEAM_COMMANDS or pending_remove_team:
            _handle_team_update_upload(message, remove=True)
            return
        if caption_lower in contact_import.TEAM_COMMANDS or pending_team:
            _handle_team_update_upload(message)
            return
        if contact_import.is_update_caption(message.caption) or pending_register:
            _handle_contact_update_upload(message)
            return

    # REGISTERED NUMBER fast path (the permanent identity layer): the
    # sender's number alone identifies the party -- no command, no caption,
    # no filename convention, no grouping window. Commands/captions from
    # registered numbers are deliberately IGNORED so a stray caption can
    # never misfile a registered party's stock. Suspendable via
    # WHATSAPP_NUMBER_REGISTRY_ENABLED (registrations are kept).
    if whatsapp_settings.registry_enabled:
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


def _handle_team_update_upload(
    message: IncomingWhatsAppMessage, *, remove: bool = False
) -> None:
    """A purchase-team list from an admin. Update mode ADDS/CORRECTS the
    listed members (rows not in the sheet are kept); remove mode DELETES the
    listed members. Team members receive every generated PO."""
    logger.info(
        "WhatsApp file '%s' from admin %s taken as the PURCHASE TEAM %s list.",
        message.filename,
        message.sender,
        "REMOVAL" if remove else "update",
    )
    try:
        client = WhatsAppClient(whatsapp_settings)
        file_path = download_document_media(message.media_id, message.filename, client)
        rows = contact_import.parse_contact_rows(Path(file_path))
        if not rows:
            send_reply_safe(
                message.sender,
                "⚠️ No rows found. Expected one row per member: Name + WhatsApp number.",
            )
            return
        with get_session() as session:
            if remove:
                reply, stats = contact_import.apply_team_removal(rows, session)
            else:
                reply, stats = contact_import.apply_team_update(rows, session)
        logger.info("Purchase team %s: %s", "removal" if remove else "update", stats)
    except Exception:
        logger.exception("Purchase team update failed for %s.", message.filename)
        send_reply_safe(
            message.sender,
            "❌ Could not read that team list. Please send an Excel with "
            "Name and WhatsApp number columns.",
        )
        return
    finally:
        with get_session() as session:
            command_store.clear_command(message.sender, session)
    send_reply_safe(message.sender, reply)


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


def _handle_customer_contact_upload(message: IncomingWhatsAppMessage) -> None:
    """A CUSTOMER contact list from an admin number: parse it and register
    the numbers against customers. The exact counterpart of
    `_handle_contact_update_upload`, reusing the same forgiving parser."""
    logger.info(
        "WhatsApp file '%s' from admin %s taken as a CUSTOMER CONTACT LIST "
        "(registry update, not an order import).",
        message.filename,
        message.sender,
    )
    try:
        client = WhatsAppClient(whatsapp_settings)
        file_path = download_document_media(message.media_id, message.filename, client)
    except Exception:
        logger.exception(
            "Could not download the customer list %s from %s.",
            message.media_id,
            message.sender,
        )
        send_reply_safe(
            message.sender, "❌ Could not receive the customer list. Please send it again."
        )
        return

    try:
        rows = contact_import.parse_contact_rows(Path(file_path))
        if not rows:
            send_reply_safe(
                message.sender,
                "⚠️ No customer rows found in that file. Expected one row per "
                "customer: Customer Name + WhatsApp number(s).",
            )
            return
        with get_session() as session:
            reply, stats = contact_import.apply_customer_contact_update(rows, session)
        logger.info("Founder customer registry update applied: %s", stats)
    except Exception:
        logger.exception("Customer registry update failed for %s.", message.filename)
        send_reply_safe(
            message.sender,
            "❌ Could not read that customer list. Please send an Excel with "
            "Customer Name and WhatsApp number columns.",
        )
        return
    finally:
        # One list per "register customer" command -- the next file from this
        # admin is a normal upload again.
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
        # This path sends its own reply below -- never two for one file.
        result = _process_staged_file(
            file_path, metadata, message.filename, message.media_id, notify_sender=False
        )
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

    # Customer identity for an UNREGISTERED sender's Customer Order works
    # exactly like the vendor flow above: the customer NAME comes from the
    # file caption or a follow-up text. Unlike vendor files this is OPTIONAL
    # -- a filename carrying a Customer Code still resolves on its own, and
    # an order with no customer at all is a supported state -- so the file is
    # only held when neither a caption nor a code-shaped filename is present.
    customer_name = None
    if command.document_type == IncomingDocumentType.CUSTOMER_ORDER:
        customer_name = (message.caption or "").strip()
        if not customer_name and not customer_code_service.parse_customer_code_from_filename(
            message.filename or ""
        ):
            with get_session() as session:
                pending_customer_files.add(
                    message.sender, str(file_path), message.filename, session
                )
            logger.info(
                "WhatsApp customer order '%s' from %s has no customer name and no "
                "Customer Code -- held at %s; asking the sender.",
                message.filename,
                message.sender,
                file_path,
            )
            send_reply_safe(
                message.sender,
                f"Got '{message.filename}'. Which customer is this order for? "
                "Reply with the customer name (e.g. Karol Bagh).",
            )
            return

    metadata = DocumentMetadata(
        sender=message.sender,
        caption=message.caption,
        external_message_id=message.message_id,
        original_filename=message.filename,
        document_type_hint=command.document_type,
        vendor_name=vendor_name or None,
        customer_name=customer_name or None,
    )
    _process_staged_file(file_path, metadata, message.filename, message.media_id)


def _sender_result_reply(result, document_type: IncomingDocumentType) -> str:
    """The reply ANY sender gets about their own file. Same plain wording the
    registered parties get, plus the REASON when the file could not be used --
    without it the sender has no idea what to fix and simply resends the same
    file."""
    text = _registered_result_reply(None, result, document_type)
    status = getattr(getattr(result, "status", None), "value", None)
    if status in ("FAILED", "DOWNLOAD_FAILED", "NEEDS_REVIEW", "UNSUPPORTED"):
        reason = (getattr(result, "message", "") or "").strip()
        if reason:
            # One line, no stack traces or file paths -- enough to act on.
            reason = reason.splitlines()[0][:180]
            text = f"{text}\nReason: {reason}"
    return text


def _notify_sender_of_result(metadata: DocumentMetadata, result) -> None:
    """Tell the person who sent the file what happened to it. Best-effort --
    a failed reply must never affect the import.

    Skipped for admin numbers: they already receive the full technical detail
    through the notification mirror, and the Founder's rule is ONE message per
    import, not two."""
    sender = (getattr(metadata, "sender", None) or "").strip()
    if not sender or result is None:
        return
    try:
        if daily_stock.is_admin_sender(sender):
            return
        document_type = getattr(result, "document_type", None) or metadata.document_type_hint
        send_reply_safe(sender, _sender_result_reply(result, document_type))
    except Exception:  # noqa: BLE001 -- an output must never affect the import
        logger.exception("Could not send the import result to %s.", sender)


def _process_staged_file(
    file_path,
    metadata: DocumentMetadata,
    display_name: str,
    media_id: str | None = None,
    *,
    notify_sender: bool = True,
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
        # A FAILED file is exactly the one the Founder needs to look at --
        # send it straight back to the admin chat (best-effort). Processing
        # has moved it to uploads/failed/ by now, so resolve its real
        # location rather than assuming the staged path.
        _send_failed_file_safe(result, "WhatsApp")
        # Whoever sent the file hears what happened to it -- imported, or
        # not used and why. (Registered senders get their own reply from
        # `_handle_registered_upload`, so that path passes notify_sender=False.)
        if notify_sender:
            _notify_sender_of_result(metadata, result)

    # Temporary Google-Sheets replacement (output layer): the import above is
    # now committed, so on a SUCCESSFUL Vendor Inventory import request the
    # consolidated all-vendor workbook for the Founder over WhatsApp. The
    # request is DEBOUNCED: a batch of vendor files produces ONE final
    # workbook (sent after the batch goes quiet), not one per file. Fully
    # best-effort -- it opens its own session and cannot affect the import.
    if _is_successful_inventory_import(result):
        # AUTO TOP-UP (Founder's rule): this vendor's new stock fills the
        # still-unfilled parts of recent customer orders -- adding only,
        # never moving an allocation that already exists. Runs here, after
        # the import transaction has COMMITTED, so the new stock is visible;
        # in its own session/transaction, and never fails the import.
        run_topup_for_vendor(
            getattr(result, "vendor_id", None), getattr(result, "vendor_name", None)
        )
        inventory_output.request_consolidated_send(getattr(result, "vendor_name", None))
        # DEALER PORTAL: push this vendor's new snapshot -- including the
        # parts that DISAPPEARED since the previous snapshot, sent as
        # quantity 0, which DP cannot work out for itself. Gated by
        # DEALER_PORTAL_ENABLED (default false) and DEALER_PORTAL_SHADOW
        # (default true); best-effort in its own session, and never raises.
        dealer_portal.request_push(
            getattr(result, "vendor_id", None), getattr(result, "vendor_name", None)
        )

    # Founder automation ("Combined ZIP" mode): a successfully imported
    # customer order is queued for automatic vendor selection; the batch runs
    # after order imports go quiet and sends ONE ZIP of allocation reports.
    order_id = _successful_customer_order_id(result)
    if order_id is not None:
        allocation_batch.request_order_allocation(order_id)

    return result


def _send_failed_file_safe(result, source: str) -> None:
    """Deliver a failed import's original file to the Founder's WhatsApp.
    Looks the file up through the inbox record (it has been moved to
    uploads/failed/ by now). Never raises."""
    try:
        status = getattr(getattr(result, "status", None), "value", None)
        if status not in failed_file.FAILURE_STATUSES:
            return
        document_id = getattr(result, "document_id", None)
        if document_id is None:
            return
        with get_session() as session:
            document = session.get(IncomingDocument, document_id)
            path = (
                documents_service.resolve_stored_file(document)
                if document is not None
                else None
            )
        failed_file.send_failed_file(result, source, path)
    except Exception:  # noqa: BLE001 -- an output must never affect the import
        logger.exception("Could not deliver the failed file for document %s.",
                         getattr(result, "document_id", None))


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
