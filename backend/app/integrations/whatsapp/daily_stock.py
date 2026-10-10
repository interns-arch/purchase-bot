"""Daily vendor stock automation, built on the number registry.

The full cycle (all times IST, all configurable):

  9:00 AM  send_morning_requests()   -- approved template to every registered
                                        vendor number: "please share stock"
  ...      vendors reply with files  -- imported automatically by number
                                        (document_worker registered fast path)
  11:00 AM send_daily_summary()      -- "Received: X of Y. Pending: ..." to
                                        the admin number
  11:30 AM send_auto_reminders()     -- optional; template to still-pending
                                        vendors only
  anytime  admin texts "send reminder" -- same reminder, on demand

"Submitted today" = the vendor has an InventoryImport created since IST
midnight whose status shows rows actually landed (COMPLETED /
COMPLETED_WITH_ERRORS, or SUPERSEDED for an earlier file replaced by a newer
one the same day). A FAILED attempt does NOT count -- the vendor tried, but
we still have no stock, so they belong on the pending list.

Every function here is scheduler-safe: never raises, logs + notifies
instead. Template sends require the template to be APPROVED in the Meta
dashboard first (WHATSAPP_STOCK_REQUEST_TEMPLATE)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp import registry
from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.outbound import send_reply_safe
from backend.app.notifications import broker
from core.db import get_session
from core.logging_setup import get_logger
from core.models import ImportStatus, InventoryImport

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_SUBMITTED_STATUSES = (
    ImportStatus.COMPLETED,
    ImportStatus.COMPLETED_WITH_ERRORS,
    ImportStatus.SUPERSEDED,
)


def _ist_today_start_utc() -> datetime:
    """IST midnight of 'today', as naive UTC (how created_at is stored)."""
    ist_now = datetime.now(_IST)
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return ist_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def vendors_submitted_today(session: Session) -> set[int]:
    """Vendor ids with a real stock submission since IST midnight."""
    rows = session.execute(
        select(InventoryImport.vendor_id)
        .where(
            InventoryImport.created_at >= _ist_today_start_utc(),
            InventoryImport.status.in_(_SUBMITTED_STATUSES),
        )
        .distinct()
    ).scalars()
    return set(rows)


def participation_today(session: Session) -> tuple[list[str], list[str], set[int]]:
    """(received_names, pending_names, pending_vendor_ids) among vendors with
    at least one registered number. Plain values only -- callers use them
    after the session has closed."""
    vendors = registry.registered_vendors(session)  # [(id, name)]
    submitted = vendors_submitted_today(session)
    received = sorted((name for vid, name in vendors if vid in submitted), key=str.lower)
    pending = sorted((name for vid, name in vendors if vid not in submitted), key=str.lower)
    pending_ids = {vid for vid, _ in vendors if vid not in submitted}
    return received, pending, pending_ids


_template_info: dict[str, tuple[str, int]] = {}
_template_info_at = 0.0


def _meta_templates() -> dict[str, tuple[str, int]]:
    """{name: (status, number of {{n}} placeholders)} from Meta, cached 10 min."""
    import os
    import re
    import time

    import httpx

    global _template_info, _template_info_at
    if _template_info and time.time() - _template_info_at < 600:
        return _template_info
    waba = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip()
    if not waba:
        return {}
    try:
        data = httpx.get(
            f"{whatsapp_settings.graph_api_base_url}/{waba}/message_templates",
            params={"fields": "name,status,components", "limit": 200},
            headers={"Authorization": f"Bearer {whatsapp_settings.access_token}"},
            timeout=15,
        ).json()
        info = {}
        for item in data.get("data", []):
            body = next((c.get("text", "") for c in item.get("components", []) if c.get("type") == "BODY"), "")
            info[item["name"]] = (item.get("status", ""), len(set(re.findall(r"\{\{(\d+)\}\}", body))))
        _template_info, _template_info_at = info, time.time()
    except Exception:  # noqa: BLE001 -- fall back to the first configured name
        logger.exception("Could not read WhatsApp template statuses from Meta.")
    return _template_info


def _pick_template(configured: str) -> tuple[str, int]:
    """`configured` may list several names, best first
    ("vendor_stock_request,stock_request"): the first one Meta has APPROVED
    is used, so a newly submitted template takes over by itself the moment
    it is approved. Returns (name, number of placeholders)."""
    names = [n.strip() for n in (configured or "").split(",") if n.strip()]
    info = _meta_templates()
    for name in names:
        status, params = info.get(name, ("", 0))
        if status == "APPROVED":
            return name, params
    fallback = names[-1] if names else configured
    return fallback, info.get(fallback, ("", 0))[1]


def _send_template_to_contacts(contacts, template_name: str) -> tuple[int, int]:
    """Send the template to each `registry.VendorContact` -- the vendor's
    name fills {{1}} when the template has one. Returns (sent, failed). One
    bad number never blocks the rest."""
    template_name, placeholders = _pick_template(template_name)
    logger.info("Stock template in use: %s (%d placeholder(s)).", template_name, placeholders)
    client = WhatsAppClient(whatsapp_settings)
    sent = failed = 0
    for contact in contacts:
        try:
            client.send_template_message(
                contact.whatsapp_number,
                template_name,
                whatsapp_settings.template_language,
                [contact.vendor_name or "Sir"] if placeholders else None,
            )
            sent += 1
        except Exception:  # noqa: BLE001 -- one undeliverable vendor must not stop the batch
            failed += 1
            logger.exception(
                "Could not send template %r to %s (%s).",
                template_name,
                contact.whatsapp_number,
                contact.vendor_name,
            )
    return sent, failed


def send_morning_requests() -> None:
    """Scheduled: the fixed-time 'please share your stock' template to every
    registered vendor number."""
    try:
        with get_session() as session:
            contacts = registry.registered_vendor_contacts(session)
        vendor_count = len({contact.vendor_id for contact in contacts})
        if not contacts:
            logger.info("Morning stock request: no registered vendor numbers -- nothing to send.")
            return
        sent, failed = _send_template_to_contacts(
            contacts, whatsapp_settings.stock_request_template
        )
        logger.info(
            "Morning stock request: %d sent, %d failed (%d vendors).", sent, failed, vendor_count
        )
        detail = f"Vendors: {vendor_count}\nMessages sent: {sent}"
        if failed:
            detail += f"\nFailed: {failed} (see server logs)"
        broker.publish(
            "warning" if failed else "info", "Morning stock request sent.", detail
        )
    except Exception:  # noqa: BLE001 -- a scheduled job must never raise
        logger.exception("Morning stock request job failed.")
        broker.publish("error", "Morning stock request failed.", "See server logs.")


def _summary_text(received: list[str], pending: list[str]) -> str:
    total = len(received) + len(pending)
    lines = [
        f" Today's Vendor Stock Status — {datetime.now(_IST).strftime('%d %b')}",
        f"Received: {len(received)} / {total}",
    ]
    if received:
        lines.append("")
        lines.append("✅ Received:")
        lines.extend(f"• {name}" for name in received)
    if pending:
        lines.append("")
        lines.append("⚠️ Pending:")
        lines.extend(f"• {name}" for name in pending)
        lines.append("")
        lines.append('Reply "send reminder" to nudge the pending vendors.')
    else:
        lines.append("")
        lines.append("🎉 Every registered vendor has submitted today.")
    return "\n".join(lines)


SUMMARY_TEMPLATE = "daily_vendor_stock_status"


def _send_summary(to: str, text: str, received: list[str], pending: list[str]) -> bool:
    """The full text when the admin is inside WhatsApp's 24 h window; the
    approved summary template (same facts) when he is not."""
    from backend.app.integrations.whatsapp.outbound import (
        flatten_for_template,
        in_service_window,
        template_approved,
    )

    if in_service_window(to) or not template_approved(SUMMARY_TEMPLATE):
        return send_reply_safe(to, text)
    try:
        WhatsAppClient(whatsapp_settings).send_template_message(
            to,
            SUMMARY_TEMPLATE,
            whatsapp_settings.template_language,
            [
                datetime.now(_IST).strftime("%d %b"),
                str(len(received)),
                str(len(received) + len(pending)),
                flatten_for_template(", ".join(received) or "none yet", 600),
                flatten_for_template(", ".join(pending) or "none", 600),
            ],
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Daily summary template to %s failed.", to)
        return False


def send_daily_summary() -> None:
    """Scheduled: the received/pending participation summary, straight to the
    admin number (plain text -- the admin talks to the bot daily, so the 24h
    window is open; no template needed)."""
    try:
        recipients = whatsapp_settings.admin_phone_numbers
        if not recipients:
            logger.info("Daily summary: WHATSAPP_ADMIN_PHONE_NUMBER not set -- skipping.")
            return
        with get_session() as session:
            received, pending, _ = participation_today(session)
        if not received and not pending:
            logger.info("Daily summary: no registered vendors yet -- nothing to report.")
            return
        text = _summary_text(received, pending)
        delivered = [_send_summary(to, text, received, pending) for to in recipients]
        if not any(delivered):
            broker.publish(
                "warning",
                "Daily vendor stock summary could not be sent to WhatsApp.",
                f"Received {len(received)}, pending {len(pending)} -- see the dashboard.",
            )
    except Exception:  # noqa: BLE001 -- a scheduled job must never raise
        logger.exception("Daily stock summary job failed.")


def send_reminders_to_pending() -> tuple[list[str], int, int]:
    """Send the reminder template to every registered number of every
    still-pending vendor. Returns (pending_names, sent, failed)."""
    with get_session() as session:
        _, pending_names, pending_ids = participation_today(session)
        contacts = [
            contact
            for contact in registry.registered_vendor_contacts(session)
            if contact.vendor_id in pending_ids
        ]
    if not contacts:
        return pending_names, 0, 0
    sent, failed = _send_template_to_contacts(contacts, whatsapp_settings.reminder_template)
    return pending_names, sent, failed


def send_auto_reminders() -> None:
    """Scheduled (optional, WHATSAPP_AUTO_REMINDER_TIME): reminder template to
    still-pending vendors, then a one-line report to the admin."""
    try:
        pending, sent, failed = send_reminders_to_pending()
        if not pending:
            logger.info("Auto reminder: no pending vendors -- nothing to send.")
            return
        names = ", ".join(pending)
        logger.info("Auto reminder sent to %d pending vendor(s): %s", len(pending), names)
        text = f"⏰ Auto reminder sent to {len(pending)} pending vendor(s):\n{names}"
        if failed:
            text += f"\n⚠️ {failed} message(s) could not be delivered."
        for to in whatsapp_settings.admin_phone_numbers:
            send_reply_safe(to, text)
    except Exception:  # noqa: BLE001 -- a scheduled job must never raise
        logger.exception("Auto reminder job failed.")


# --- Admin "send reminder" text command -----------------------------------

_REMINDER_COMMANDS = {"send reminder", "send reminders", "reminder", "remind vendors"}


def is_admin_sender(sender: str) -> bool:
    """Whether `sender` is one of the configured admin/founder numbers."""
    normalized = registry.normalize_number(sender)
    return any(
        normalized == registry.normalize_number(admin)
        for admin in whatsapp_settings.admin_phone_numbers
    )


def is_reminder_command(sender: str, text: str | None) -> bool:
    """True when an ADMIN number texts a reminder command. Checked before
    any other text handling so it can never be mistaken for a vendor name."""
    if not is_admin_sender(sender):
        return False
    return (text or "").strip().lower() in _REMINDER_COMMANDS


def handle_reminder_command(sender: str) -> None:
    """Run the on-demand reminder and reply to the admin with the outcome."""
    try:
        pending, sent, failed = send_reminders_to_pending()
        if not pending:
            send_reply_safe(sender, "🎉 All registered vendors have already submitted today's stock.")
            return
        names = "\n".join(f"• {name}" for name in pending)
        text = f"Sending stock reminder to {len(pending)} pending vendor(s):\n{names}"
        if failed:
            text += f"\n⚠️ {failed} message(s) could not be delivered (see server logs)."
        send_reply_safe(sender, text)
    except Exception:  # noqa: BLE001 -- a command failure must not crash the worker
        logger.exception("'send reminder' command failed.")
        send_reply_safe(sender, "❌ Could not send reminders -- see server logs.")
