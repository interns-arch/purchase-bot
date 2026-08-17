"""Background Automation: a single in-process APScheduler instance for
periodic jobs (currently just Gmail polling -- Google Sheets sync is
event-triggered from the inventory import path instead, not polled, see
`core/services/google_sheets_sync_service.py`). Started/stopped from
`backend/app/main.py`'s FastAPI `lifespan`, so no separate worker process or
external cron is needed to run this app's automation on a single Render web
service.

Each scheduled job call is wrapped so an exception never kills the
scheduler thread -- the job functions themselves
(`email_worker.poll_gmail_inbox`) already catch broadly, but this is a
last-resort backstop."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from backend.app.integrations.gmail.config import gmail_settings
from backend.app.integrations.google_sheets.config import google_sheets_settings
from backend.app.integrations.google_sheets.sync_service import reset_sheet_for_new_day_safe
from backend.app.integrations.whatsapp import daily_stock
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.workers.email_worker import poll_gmail_inbox
from core.logging_setup import get_logger

logger = get_logger(__name__)

_scheduler = BackgroundScheduler(daemon=True)

_IST_TZ = "Asia/Kolkata"


def _run_safely(job_name: str, job) -> None:
    try:
        job()
    except Exception:  # noqa: BLE001 -- a scheduled job must never kill the scheduler thread
        logger.exception("Scheduled job '%s' failed.", job_name)


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """'09:00' -> (9, 0); None for blank/invalid (job simply not scheduled)."""
    try:
        hour_text, _, minute_text = value.strip().partition(":")
        hour, minute = int(hour_text), int(minute_text or "0")
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    return None


def _add_daily_ist_job(job_id: str, time_text: str, job) -> None:
    parsed = _parse_hhmm(time_text)
    if parsed is None:
        logger.warning("Daily job '%s' NOT scheduled -- invalid time %r.", job_id, time_text)
        return
    hour, minute = parsed
    _scheduler.add_job(
        lambda: _run_safely(job_id, job),
        "cron",
        hour=hour,
        minute=minute,
        timezone=_IST_TZ,
        id=job_id,
        replace_existing=True,
    )
    logger.info("Daily job '%s' scheduled at %02d:%02d IST.", job_id, hour, minute)


def _schedule_whatsapp_daily_jobs() -> None:
    """The daily vendor-stock cycle (see integrations/whatsapp/daily_stock.py).
    Each job is opt-in via its own setting; all times are IST."""
    if not whatsapp_settings.enabled:
        return
    if whatsapp_settings.daily_request_enabled:
        _add_daily_ist_job(
            "whatsapp_morning_stock_request",
            whatsapp_settings.daily_request_time,
            daily_stock.send_morning_requests,
        )
    if whatsapp_settings.daily_summary_enabled and whatsapp_settings.admin_phone_number:
        _add_daily_ist_job(
            "whatsapp_daily_stock_summary",
            whatsapp_settings.daily_summary_time,
            daily_stock.send_daily_summary,
        )
    if whatsapp_settings.auto_reminder_time:
        _add_daily_ist_job(
            "whatsapp_auto_stock_reminder",
            whatsapp_settings.auto_reminder_time,
            daily_stock.send_auto_reminders,
        )


def _schedule_google_sheet_daily_reset() -> None:
    """Founder rule: the Google Sheet shows ONLY same-day uploads -- every
    morning, vendor tabs without a submission today are removed."""
    if google_sheets_settings.enabled and google_sheets_settings.daily_reset_enabled:
        _add_daily_ist_job(
            "google_sheet_daily_reset",
            google_sheets_settings.daily_reset_time,
            reset_sheet_for_new_day_safe,
        )


def _schedule_startup_recovery() -> None:
    """One-shot, shortly after boot: re-queue customer orders whose
    allocation was lost to a crash/restart (the in-memory batch queue does
    not survive one -- see workers/recovery.py)."""
    from datetime import datetime, timedelta, timezone

    from backend.app.workers import recovery

    _scheduler.add_job(
        lambda: _run_safely(
            "requeue_unallocated_orders", recovery.requeue_unallocated_recent_orders
        ),
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="startup_allocation_recovery",
        replace_existing=True,
    )


def start_scheduler() -> None:
    _schedule_whatsapp_daily_jobs()
    _schedule_google_sheet_daily_reset()
    _schedule_startup_recovery()

    if gmail_settings.enabled:
        _scheduler.add_job(
            lambda: _run_safely("poll_gmail_inbox", poll_gmail_inbox),
            "interval",
            seconds=gmail_settings.poll_interval_seconds,
            id="poll_gmail_inbox",
            replace_existing=True,
        )
        logger.info(
            "Gmail automation enabled -- polling every %ds (auth_mode=%s).",
            gmail_settings.poll_interval_seconds,
            gmail_settings.auth_mode,
        )
    else:
        logger.info("Gmail automation disabled (ENABLE_EMAIL_AUTOMATION/GMAIL_ENABLED not set).")

    if _scheduler.get_jobs():
        _scheduler.start()


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
