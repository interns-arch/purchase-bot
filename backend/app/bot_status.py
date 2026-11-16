"""Is the Purchase Bot working? (Founder, 3 Nov 2026: "sometimes the bot suddenly
stops working, but there is no notification".) `problems()` is what the bot knows
about itself, served at /api/bot-status. `backend/scripts/bot_watch.py`, a separate
container, reads it and sends the alerts, so it can also report the bot not
answering at all.

Each problem is {key, text, repeat}: `text` is shown to the Founder, `repeat` says
whether it is worth reminding about while it lasts.

A closed 24-hour window or WhatsApp's marketing limit is one person missing one
message (32 of each in 3 days), not the bot failing. Those alone never raise an
alert, unless everything is failing or nobody got a purchase plan."""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import timedelta

from sqlalchemy import func, select, text

from core.logging_setup import get_logger

logger = get_logger(__name__)

_SUCCESS = ("sent", "delivered", "read")
# WhatsApp refusing the whole account, not one person: payment (131042), account
# locked (131031), spam restriction (368, 130497), and a bad or expired token (190).
_ACCOUNT_ERROR = re.compile(r"^(131042|131031|368|130497|190)\b")
_PLAN_PREFIX = "🛒 Purchase plan"
_PLAN_BATCH_MINUTES = 5

_lock = threading.Lock()
_last_beat = time.monotonic()  # counts from start-up, so a fresh process is not "stopped"
_failed_jobs: dict[str, float] = {}


def _int_env(name: str, default: int) -> int:
    return int((os.environ.get(name) or str(default)).strip())


# ------------------------------------------------------------------ fed by the scheduler
def beat() -> None:
    global _last_beat
    with _lock:
        _last_beat = time.monotonic()


def job_ok(name: str) -> None:
    with _lock:
        _failed_jobs.pop(name, None)


def job_failed(name: str) -> None:
    with _lock:
        _failed_jobs[name] = time.monotonic()


def _scheduler_problems() -> list[dict]:
    with _lock:
        idle = time.monotonic() - _last_beat
        failed = sorted(_failed_jobs)
    found = []
    if idle > _int_env("BOT_STATUS_SCHEDULER_STALE_SECONDS", 300):
        found.append(_problem("scheduler", "its scheduled work has stopped running"))
    if failed:
        found.append(_problem("jobs", "scheduled work is failing: " + ", ".join(failed)))
    return found


def _problem(key: str, text_: str, *, repeat: bool = True) -> dict:
    return {"key": key, "text": text_, "repeat": repeat}


# ------------------------------------------------------------------ WhatsApp delivery
def _delivery_problems(session, now) -> list[dict]:
    from backend.app.integrations.whatsapp.models import WhatsAppChatMessage as Chat

    found = []
    minutes = _int_env("BOT_STATUS_DELIVERY_WINDOW_MINUTES", 60)
    recent = session.execute(
        select(Chat.status, Chat.error)
        .where(Chat.direction == "out", Chat.created_at >= now - timedelta(minutes=minutes))
        .order_by(Chat.id)
    ).all()
    succeeded = sum(1 for status, _ in recent if status in _SUCCESS)
    failed = [error or "" for status, error in recent if status == "failed"]

    # The newest report decides: a later good send means it has been sorted out.
    last_status, last_error = recent[-1] if recent else (None, None)
    if last_status == "failed" and _ACCOUNT_ERROR.match(last_error or ""):
        found.append(_problem("whatsapp_account", f"WhatsApp is refusing its messages ({last_error[:60]})"))
    elif not succeeded and len(failed) >= _int_env("BOT_STATUS_MIN_FAILED", 5):
        found.append(_problem("whatsapp_sending", f"none of its last {len(failed)} WhatsApp messages were delivered"))

    plan = _undelivered_plan(session)
    if plan:
        # Once per plan: a reminder would not get it delivered.
        found.append(_problem(f"plan_{plan}", f"nobody received the purchase plan sent at {plan}", repeat=False))
    return found


def _undelivered_plan(session) -> str | None:
    """The time of the newest purchase plan, when every copy of it failed."""
    from backend.app.integrations.whatsapp.models import WhatsAppChatMessage as Chat

    newest = session.execute(
        select(func.max(Chat.created_at)).where(Chat.direction == "out", Chat.text.startswith(_PLAN_PREFIX))
    ).scalar()
    if newest is None:
        return None
    copies = session.execute(
        select(Chat.status).where(
            Chat.direction == "out",
            Chat.text.startswith(_PLAN_PREFIX),
            Chat.created_at >= newest - timedelta(minutes=_PLAN_BATCH_MINUTES),
        )
    ).scalars().all()
    return None if any(status != "failed" for status in copies) else newest.strftime("%d %b %H:%M")


# ------------------------------------------------------------------ the answer
def problems() -> list[dict]:
    """Everything wrong right now; empty means working. Never raises: not being
    able to read the database is itself the answer."""
    from core.db import get_session
    from core.time_utils import now_ist_naive

    found = _scheduler_problems()
    try:
        with get_session() as session:
            session.execute(text("SELECT 1"))
            found += _delivery_problems(session, now_ist_naive())
    except Exception:  # noqa: BLE001
        logger.exception("Bot status: could not read the database.")
        found.append(_problem("database", "it cannot reach its database"))
    return found
