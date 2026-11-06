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

from backend.app.integrations.dealer_portal.config import dealer_portal_settings
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
    # Nothing a scheduled job sends goes into a WhatsApp group: the daily
    # stock status, Gmail-order purchase plans and the like reach each person
    # on their own chat (2 Nov 2026: "Today's Vendor Stock Status" appeared
    # in the team group as Rajkumar). The group stock request posts on its
    # own, outside this routing, so it is unaffected.
    from backend.app import bot_status
    from backend.app.integrations.whatsapp import whapi

    try:
        with whapi.direct_only():
            job()
        bot_status.job_ok(job_name)
    except Exception:  # noqa: BLE001 -- a scheduled job must never kill the scheduler thread
        logger.exception("Scheduled job '%s' failed.", job_name)
        bot_status.job_failed(job_name)


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
    from backend.app.advance_orders import approval

    # Purchase approval mode: the bot sends vendors nothing of its own.
    vendor_jobs = not approval.enabled()
    if whatsapp_settings.daily_request_enabled and vendor_jobs:
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
    if whatsapp_settings.auto_reminder_time and vendor_jobs:
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
    from backend.app.integrations.google_sheets import dealer_stock

    if google_sheets_settings.enabled and dealer_stock.ALL_STOCK_ENABLED:
        # ALL STOCK is emptied every evening (team, 29 Oct 2026).
        _add_daily_ist_job("google_sheet_all_stock_clear", dealer_stock.ALL_STOCK_CLEAR_TIME, dealer_stock.clear_all_stock_safe)


def _schedule_group_stock_requests() -> None:
    """"Stock share kar dijiye" in every vendor WhatsApp group (team, 29 Oct
    2026) -- independent of purchase approval mode."""
    from backend.app.integrations.whatsapp import group_stock_request

    if not group_stock_request.enabled():
        return
    for at in group_stock_request.request_times():
        _add_daily_ist_job(f"whapi_group_stock_request_{at.replace(':', '')}", at, group_stock_request.send_requests_safe)


def _schedule_mrp_requests() -> None:
    """"MRP list share kar dijiye" in every vendor WhatsApp group, once a month
    (Founder, 2 Nov 2026)."""
    from backend.app.integrations.whatsapp import mrp_list

    if not mrp_list.asking_enabled():
        return
    parsed = _parse_hhmm(mrp_list.request_time())
    if parsed is None:
        logger.warning("Monthly MRP request NOT scheduled -- invalid time %r.", mrp_list.request_time())
        return
    _scheduler.add_job(
        lambda: _run_safely("whapi_monthly_mrp_request", mrp_list.send_requests_safe),
        "cron",
        day=mrp_list.request_day(),
        hour=parsed[0],
        minute=parsed[1],
        timezone=_IST_TZ,
        id="whapi_monthly_mrp_request",
        replace_existing=True,
    )
    logger.info("Monthly MRP request scheduled on day %d at %02d:%02d IST.", mrp_list.request_day(), *parsed)


def _schedule_dealer_portal_retry() -> None:
    """Re-send Dealer Portal pushes left in FAILED (a DP outage, a timeout,
    an auth failure). Safe to repeat: DP's upload is an absolute replace, so
    re-sending the same stock cannot double-count it.

    Does nothing unless DEALER_PORTAL_ENABLED is set."""
    if not dealer_portal_settings.enabled:
        return
    if dealer_portal_settings.retry_interval_minutes <= 0:
        return

    from backend.app.integrations import dealer_portal

    _scheduler.add_job(
        lambda: _run_safely(
            "dealer_portal_retry_failed_pushes", dealer_portal.retry_failed_pushes
        ),
        "interval",
        minutes=dealer_portal_settings.retry_interval_minutes,
        id="dealer_portal_retry_failed_pushes",
        replace_existing=True,
    )
    logger.info(
        "Dealer Portal retry sweep enabled -- every %d minute(s), shadow=%s.",
        dealer_portal_settings.retry_interval_minutes,
        dealer_portal_settings.shadow,
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


def _schedule_advance_orders() -> None:
    """Advance orders from the sales bot: send queued vendor questions inside
    vendor hours and move past vendors who did not answer in time. Does
    nothing unless ADVANCE_ORDERS_ENABLED=true."""
    from backend.app.advance_orders.config import advance_order_settings

    if not advance_order_settings.enabled:
        return

    def _tick() -> None:
        from backend.app.advance_orders import service
        from core.db import get_session

        with get_session() as session:
            service.tick(session)

    _scheduler.add_job(
        lambda: _run_safely("advance_orders_tick", _tick),
        "interval",
        minutes=advance_order_settings.tick_minutes,
        id="advance_orders_tick",
        replace_existing=True,
    )
    logger.info("Advance orders enabled -- vendor questions checked every %d minute(s).", advance_order_settings.tick_minutes)


def _schedule_ai_learning() -> None:
    """Nightly review of the WhatsApp assistant's chats: Gemini turns the
    day's misunderstandings into short lessons used in every later chat (see
    `vendor_onboarding.learning`). Runs only while vendor onboarding is on."""
    from backend.app.vendor_onboarding.config import vendor_onboarding_settings

    if not vendor_onboarding_settings.enabled:
        return
    from backend.app.vendor_onboarding import learning

    _scheduler.add_job(
        lambda: _run_safely("ai_daily_review", learning.daily_review),
        "cron",
        hour=21,
        minute=0,
        timezone=_IST_TZ,
        id="ai_daily_review",
        replace_existing=True,
    )
    logger.info("AI learning enabled -- the assistant reviews its chats daily at 21:00 IST.")


def _schedule_part_brand_refresh() -> None:
    """Advance orders route a part to its brand's vendors; the part -> brand
    list is rebuilt nightly from the Brand column of that day's stock files."""
    from backend.app.advance_orders.config import advance_order_settings

    if not advance_order_settings.enabled:
        return

    def _refresh() -> None:
        from backend.app.advance_orders.brands import refresh_hints_from_stock
        from core.db import get_session

        with get_session() as session:
            refresh_hints_from_stock(session)

    _scheduler.add_job(
        lambda: _run_safely("part_brand_refresh", _refresh),
        "cron",
        hour=22,
        minute=0,
        timezone=_IST_TZ,
        id="part_brand_refresh",
        replace_existing=True,
    )


def _schedule_heartbeat() -> None:
    """Proof the scheduler is still running, read by /api/bot-status."""
    from backend.app import bot_status

    _scheduler.add_job(bot_status.beat, "interval", seconds=60, id="bot_status_heartbeat", replace_existing=True)


def start_scheduler() -> None:
    _schedule_heartbeat()
    _schedule_part_brand_refresh()
    _schedule_ai_learning()
    _schedule_whatsapp_daily_jobs()
    _schedule_advance_orders()
    _schedule_google_sheet_daily_reset()
    _schedule_group_stock_requests()
    _schedule_mrp_requests()
    _schedule_dealer_portal_retry()
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
