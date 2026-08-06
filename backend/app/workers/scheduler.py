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
from backend.app.workers.email_worker import poll_gmail_inbox
from core.logging_setup import get_logger

logger = get_logger(__name__)

_scheduler = BackgroundScheduler(daemon=True)


def _run_safely(job_name: str, job) -> None:
    try:
        job()
    except Exception:  # noqa: BLE001 -- a scheduled job must never kill the scheduler thread
        logger.exception("Scheduled job '%s' failed.", job_name)


def start_scheduler() -> None:
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
