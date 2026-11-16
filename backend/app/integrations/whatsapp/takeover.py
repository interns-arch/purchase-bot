"""HUMAN TAKEOVER of a purchase-bot chat (founder, 28 Oct 2026 -- the same
thing the sales bot's console has).

    ON   the bot sends NOTHING automatic to that number: every send goes
         through `client._logged`, which asks `blocks()` first. What it wanted
         to say is kept on the chat (`held`) for the admin to send or drop.
         Only a send made inside `as_admin()` goes through. Texts the person
         sends are kept (`pending`, and the AI's memory) and not answered.
         FILES still import -- the stock upload is the bot's first job -- and
         only the bot's reply about them is held.
    OFF  the latest unanswered text is answered ONCE, through the bot's own
         text handling, with everything said meanwhile in its memory:
           nothing pending (the admin replied after it)  -> silence
           the latest is only thanks / ok / 👍            -> silence
           anything else                                 -> the bot answers it

PENDING = what they sent after our last SUCCESSFUL send. A send WhatsApp
refused clears nothing. An admin's reply clears everything pending before it.
Every step here is best-effort: a takeover problem must never stop the bot.
"""

from __future__ import annotations

import contextvars
import os
import re
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import select

from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

MAX_PENDING = 20
MAX_HELD = 30
MAX_AUDIT = 200

_admin: contextvars.ContextVar[dict | None] = contextvars.ContextVar("takeover_admin", default=None)

# Only thanks / ok / 👍 -- configurable, and a "?" always makes it a question.
_ACK_WORDS = {
    w.strip().lower()
    for w in (
        os.environ.get("TAKEOVER_ACK_WORDS")
        or "thanks,thank you,thanku,thnx,thx,ty,ok,okay,ok ji,ok thanks,okay thanks,theek hai,thik hai,"
        "shukriya,dhanyawad,dhanyavad,👍,🙏,👌,done,hmm,acha,achha,ji,ji sir,noted"
    ).split(",")
    if w.strip()
}
_ADDRESS = {"bhai", "ji", "sir", "bhaiya", "sahab", "you", "very", "much", "so", "madam"}


def key(number: str | None) -> str:
    from backend.app.integrations.whatsapp.registry import normalize_number

    return normalize_number(number or "") or ""


def _iso(value: datetime | None) -> str | None:
    from core.time_utils import ist_isoformat

    return ist_isoformat(value) if value else None


def _row(session, number: str, *, create: bool = False, lock: bool = False):
    from backend.app.integrations.whatsapp.models import WhatsAppTakeover

    query = select(WhatsAppTakeover).where(WhatsAppTakeover.whatsapp_number == number)
    if lock:
        query = query.with_for_update()
    row = session.execute(query).scalar_one_or_none()
    if row is None and create:
        row = WhatsAppTakeover(whatsapp_number=number, active=False, version=0, pending=[], held=[], audit=[])
        session.add(row)
        session.flush()
    return row


def _audit(row, event: str, by: str | None, **extra) -> None:
    entries = list(row.audit or [])
    entries.append({"at": _iso(now_ist_naive()), "event": event, "by": by, **extra})
    row.audit = entries[-MAX_AUDIT:]


# ------------------------------------------------------------------ who sends
def active(number: str | None) -> bool:
    k = key(number)
    if not k:
        return False
    try:
        from core.db import get_session

        with get_session() as session:
            row = _row(session, k)
            return bool(row and row.active)
    except Exception:  # noqa: BLE001 -- never stop a send over this
        logger.exception("Takeover check failed for %s -- treated as not taken over.", k)
        return False


def blocks(number: str | None) -> bool:
    """Is this send to be stopped? Only an automatic one, to a chat taken over."""
    return _admin.get() is None and active(number)


def admin_name() -> str | None:
    ctx = _admin.get()
    return ctx["by"] if ctx else None


@contextmanager
def as_admin(by: str, *, notice: bool = False):
    """Sends inside this block are the admin's own and pass the takeover.
    `notice`: a held-back bot message the admin let through -- it tells the
    person something, it does not answer what they asked."""
    token = _admin.set({"by": by or "admin", "notice": notice})
    try:
        yield
    finally:
        _admin.reset(token)


# ------------------------------------------------------------------ in and out
def hold_incoming(number: str, text: str, wamid: str | None) -> bool:
    """A text arrived. True when the chat is taken over: it is kept, put in
    the AI's memory of the conversation, and no handler answers it."""
    k = key(number)
    if not k:
        return False
    try:
        from core.db import get_session

        with get_session() as session:
            row = _row(session, k)
            if not row or not row.active:
                return False
            entry = {"id": wamid or f"in-{now_ist_naive().timestamp()}", "at": _iso(now_ist_naive()), "text": (text or "")[:500]}
            if any(p.get("id") == entry["id"] for p in (row.pending or [])):
                return True  # a re-delivered webhook
            row.pending = (list(row.pending or []) + [entry])[-MAX_PENDING:]
            holder = row.by or "admin"
        _remember(k, "in", text)
        logger.info("Takeover %s: held (taken over by %s) -- %s", k, holder, (text or "")[:80])
        return True
    except Exception:  # noqa: BLE001 -- unsure: let the bot handle it as before
        logger.exception("Takeover hold failed for %s -- the bot handles the message.", k)
        return False


def note_outgoing(number: str) -> None:
    """A send WhatsApp took. The admin's own reply answers what was pending."""
    ctx = _admin.get()
    if not ctx or ctx.get("notice"):
        return
    k = key(number)
    try:
        from core.db import get_session

        with get_session() as session:
            row = _row(session, k)
            if row and row.pending:
                row.handled = {"by": "admin", "admin": ctx["by"], "at": _iso(now_ist_naive()), "ids": [p.get("id") for p in row.pending]}
                row.pending = []
    except Exception:  # noqa: BLE001
        logger.exception("Takeover: could not mark %s answered.", k)


def park(number: str, kind: str, text: str | None, filename: str | None) -> None:
    """What the bot wanted to send while the chat was taken over: kept for the
    admin. A text can be sent from the Chats page as it is; a file is listed
    so the admin knows it exists."""
    k = key(number)
    try:
        from core.db import get_session

        with get_session() as session:
            row = _row(session, k)
            if not row:
                return
            shown = text or ""
            if kind in ("document", "image") and filename:
                shown = f"{filename}{' — ' + text if text else ''}"
            now = now_ist_naive()
            entry = {
                "id": f"HB-{int(now.timestamp() * 1000):x}-{len(row.held or [])}",
                "at": _iso(now),
                "kind": kind,
                "text": shown[:2000],
                "sendable": kind == "text" and bool((text or "").strip()),
            }
            row.held = (list(row.held or []) + [entry])[-MAX_HELD:]
        logger.info("Takeover %s: bot %s held back -- %s", k, kind, (text or filename or "")[:80])
    except Exception:  # noqa: BLE001
        logger.exception("Takeover: could not keep the held-back %s for %s.", kind, k)


def _remember(number: str, role: str, text: str) -> None:
    """Into the AI's memory of the conversation (staff_assistant), so it
    carries on from what was said while the bot was away."""
    if not text:
        return
    try:
        from backend.app.ai.staff_assistant import _remember as remember

        remember(number, role, text)
    except Exception:  # noqa: BLE001 -- memory is a help, never a gate
        logger.exception("Takeover: could not add to the AI memory of %s.", number)


# ------------------------------------------------------------------ admin acts
def send_admin_text(number: str, text: str, by: str) -> str | None:
    """The admin's own reply. Raises when WhatsApp refused it -- then nothing
    pending is marked answered."""
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    k = key(number)
    with as_admin(by):
        wamid = WhatsAppClient(whatsapp_settings).send_text_message(k, text)
    _remember(k, "out", f"(typed by {by}, a person at our purchase desk) {text}")
    _log_event(k, "admin reply", by)
    return wamid


def send_held(number: str, held_id: str, by: str) -> dict:
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings
    from core.db import get_session

    k = key(number)
    with get_session() as session:
        row = _row(session, k)
        entry = next((h for h in (row.held or []) if h.get("id") == held_id), None) if row else None
        if not entry:
            return {"ok": False, "error": "not found — already sent or dropped"}
        if not entry.get("sendable"):
            return {"ok": False, "error": f"only a text can be sent from here — send the {entry.get('kind')} by hand"}
    with as_admin(by, notice=True):
        WhatsAppClient(whatsapp_settings).send_text_message(k, entry["text"])
    _remember(k, "out", entry["text"])
    with get_session() as session:
        row = _row(session, k)
        row.held = [h for h in (row.held or []) if h.get("id") != held_id]
        _audit(row, "held sent", by, kind=entry.get("kind"))
    return {"ok": True, "state": view(k)}


def drop_held(number: str, held_id: str, by: str) -> dict:
    from core.db import get_session

    k = key(number)
    with get_session() as session:
        row = _row(session, k)
        entry = next((h for h in (row.held or []) if h.get("id") == held_id), None) if row else None
        if not entry:
            return {"ok": False, "error": "not found — already sent or dropped"}
        row.held = [h for h in (row.held or []) if h.get("id") != held_id]
        _audit(row, "held dropped", by, kind=entry.get("kind"))
    return {"ok": True, "state": view(k)}


def _log_event(number: str, event: str, by: str | None, **extra) -> None:
    try:
        from core.db import get_session

        with get_session() as session:
            row = _row(session, number)
            if row:
                _audit(row, event, by, **extra)
    except Exception:  # noqa: BLE001
        logger.exception("Takeover: could not write the audit for %s.", number)


def set_mode(number: str, on: bool, *, by: str, version: int | None = None) -> dict:
    """ON / OFF by a named admin. `version` is what the page last showed: a
    switch made on an older view (another admin moved it since) is refused."""
    from core.db import get_session

    k = key(number)
    if not k:
        return {"ok": False, "error": "no number"}
    with get_session() as session:
        row = _row(session, k, create=True, lock=True)
        if version is not None and int(version) != int(row.version or 0):
            return {"ok": False, "conflict": True, "state": _view_row(row)}
        if bool(row.active) == bool(on):
            return {"ok": True, "unchanged": True, "state": _view_row(row)}
        now = now_ist_naive()
        row.active = bool(on)
        row.version = int(row.version or 0) + 1
        row.by = by
        row.changed_at = now
        if on:
            row.since = now
            row.resume = None
        else:
            row.handed_back_at = now
        _audit(row, "takeover on" if on else "takeover off", by)
        state = _view_row(row)
    logger.info("Takeover %s: %s by %s", k, "ON" if on else "OFF", by)
    return {"ok": True, "state": state}


def is_ack_only(text: str) -> bool:
    raw = str(text or "")
    s = re.sub(r"\s+", " ", re.sub(r"[!.,]+", " ", raw.lower())).strip()
    if not s or "?" in raw:
        return False
    if s in _ACK_WORDS:
        return True
    words = {w for phrase in _ACK_WORDS for w in phrase.split()} | _ADDRESS
    return all(w in words for w in s.split())


def hand_back(number: str, *, by: str, version: int | None = None) -> dict:
    """Back to the bot, and answer what is still unanswered -- once.
    -> {"ok", "result": "nothing pending" | "acknowledgment" | "answered" |
        "already with the bot" | "still taken over" | "failed", "state"}"""
    from core.db import get_session

    k = key(number)
    res = set_mode(k, False, by=by, version=version)
    if not res.get("ok"):
        return res
    if res.get("unchanged"):
        return {**res, "result": "already with the bot"}

    with get_session() as session:
        row = _row(session, k, lock=True)
        pending = list(row.pending or [])
        if row.resume and row.resume.get("status") == "processing":
            return {"ok": True, "result": "already running", "state": _view_row(row)}
        if not pending:
            row.resume = {"status": "done", "result": "nothing pending", "at": _iso(now_ist_naive())}
            _audit(row, "handover", by, result="nothing pending")
            return {"ok": True, "result": "nothing pending", "state": _view_row(row)}
        latest = pending[-1]
        if is_ack_only(latest.get("text")):
            row.handled = {"by": "ack", "at": _iso(now_ist_naive()), "ids": [p.get("id") for p in pending]}
            row.pending = []
            row.resume = {"status": "done", "result": "acknowledgment", "at": _iso(now_ist_naive())}
            _audit(row, "handover", by, result="acknowledgment")
            return {"ok": True, "result": "acknowledgment", "state": _view_row(row)}
        # Claimed before the bot runs: a second hand-back finds it running.
        row.resume = {"status": "processing", "at": _iso(now_ist_naive()), "message": latest.get("id")}

    result = _replay(k, latest)
    with get_session() as session:
        row = _row(session, k, lock=True)
        if result == "answered" and row.active:
            result = "still taken over"  # taken over again while it ran: its reply was held
        if result == "answered":
            row.handled = {"by": "bot", "at": _iso(now_ist_naive()), "ids": [p.get("id") for p in pending]}
            row.pending = [p for p in (row.pending or []) if p.get("id") not in {q.get("id") for q in pending}]
        row.resume = {"status": "done", "result": result, "at": _iso(now_ist_naive())}
        _audit(row, "handover", by, result=result)
        return {"ok": True, "result": result, "state": _view_row(row)}


def _replay(number: str, entry: dict) -> str:
    """The bot's own text handling on the latest held message, as if it had
    just arrived. Its memory already has it (hold_incoming), so that copy is
    taken out first -- the handler records it again."""
    try:
        _forget_last_in(number, entry.get("text") or "")
        from backend.app.integrations.whatsapp.parser import IncomingWhatsAppText
        from backend.app.workers import document_worker

        document_worker.handle_incoming_whatsapp_text(
            IncomingWhatsAppText(sender=number, message_id=f"{entry.get('id')}#resumed", text=entry.get("text") or "")
        )
        return "answered"
    except Exception:  # noqa: BLE001
        logger.exception("Takeover %s: the bot failed on the held message.", number)
        return "failed"


def _forget_last_in(number: str, text: str) -> None:
    try:
        from backend.app.integrations.whatsapp.models import AiConversationMessage
        from core.db import get_session

        with get_session() as session:
            row = session.execute(
                select(AiConversationMessage)
                .where(AiConversationMessage.whatsapp_number == number, AiConversationMessage.role == "in")
                .order_by(AiConversationMessage.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None and row.text == (text or "")[:2000]:
                session.delete(row)
    except Exception:  # noqa: BLE001
        logger.exception("Takeover: could not tidy the AI memory of %s.", number)


def on_boot() -> None:
    """A hand-back cut off by a restart is not run again by itself: its
    messages stay pending and the Chats page shows them unanswered."""
    try:
        from backend.app.integrations.whatsapp.models import WhatsAppTakeover
        from core.db import get_session

        with get_session() as session:
            for row in session.execute(select(WhatsAppTakeover)).scalars():
                if row.resume and row.resume.get("status") == "processing":
                    row.resume = {**row.resume, "status": "interrupted"}
    except Exception:  # noqa: BLE001
        logger.exception("Takeover: boot check failed.")


# ------------------------------------------------------------------ views
def _view_row(row) -> dict:
    if row is None:
        return {"on": False, "version": 0, "pending": [], "held": []}
    return {
        "on": bool(row.active),
        "version": int(row.version or 0),
        "by": row.by,
        "since": _iso(row.since),
        "changed_at": _iso(row.changed_at),
        "handed_back_at": _iso(row.handed_back_at),
        "pending": [{"id": p.get("id"), "at": p.get("at"), "text": p.get("text")} for p in (row.pending or [])],
        "held": list(row.held or []),
        "handled": row.handled,
        "resume": row.resume,
    }


def view(number: str) -> dict:
    from core.db import get_session

    try:
        with get_session() as session:
            return _view_row(_row(session, key(number)))
    except Exception:  # noqa: BLE001
        logger.exception("Takeover view failed for %s.", number)
        return {"on": False, "version": 0, "pending": [], "held": []}


def summary() -> dict[str, dict]:
    """number -> {"on", "pending", "held"} for every chat with something to show."""
    from backend.app.integrations.whatsapp.models import WhatsAppTakeover
    from core.db import get_session

    out: dict[str, dict] = {}
    try:
        with get_session() as session:
            for row in session.execute(select(WhatsAppTakeover)).scalars():
                if row.active or row.pending or row.held:
                    out[row.whatsapp_number] = {"on": bool(row.active), "pending": len(row.pending or []), "held": len(row.held or [])}
    except Exception:  # noqa: BLE001
        logger.exception("Takeover summary failed.")
    return out
