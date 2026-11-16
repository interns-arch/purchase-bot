"""How the WhatsApp assistant gets better day by day.

The model itself cannot be retrained, so the bot learns the way a new
employee does -- by keeping notes:

1. EVERY AI-handled message is logged (`log_turn`): what the person wrote,
   what the AI understood, and which values the validators rejected.
2. GOOD EXAMPLES: messages that were understood and fully accepted are shown
   to the AI as worked examples in later prompts (`examples_for_prompt`), so
   phrasings real vendors use ("number yahi hai", "freight hum dete hai") are
   read correctly from then on.
3. NIGHTLY REVIEW (`daily_review`, 21:00 IST): Gemini reads the day's chats
   that went wrong -- rejected values, the same question asked twice,
   cancellations, people who gave up -- and writes a few short LESSONS. Active
   lessons are added to every prompt (`lessons_for_prompt`). The new lessons
   are sent to the admins on WhatsApp, and any lesson can be switched off
   with "lesson off <id>".

Safety: lessons and examples only change how messages are READ and WORDED.
Whether a GSTIN, PAN or IFSC is accepted is still decided by the validators,
so nothing the bot "learns" can let wrong data in."""

from __future__ import annotations

import json
import re
import threading
from datetime import timedelta

from sqlalchemy import select

from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

MAX_LESSONS_IN_PROMPT = 15
MAX_EXAMPLES_IN_PROMPT = 6
MAX_NEW_LESSONS_PER_DAY = 5

_pending = threading.local()


# ------------------------------------------------------------------ logging


def log_turn(
    number: str,
    flow: str,
    user_text: str,
    *,
    asked_field: str | None = None,
    intent: str | None = None,
    fields: dict | None = None,
    problems: list | None = None,
    reply: str | None = None,
) -> None:
    """Queue one turn; written by `flush()` after the caller's transaction."""
    if not hasattr(_pending, "rows"):
        _pending.rows = []
    _pending.rows.append(
        dict(
            whatsapp_number=number,
            flow=flow,
            user_text=(user_text or "")[:2000],
            asked_field=asked_field,
            intent=intent,
            fields={k: (v if isinstance(v, (str, int, float, type(None))) else str(v)) for k, v in (fields or {}).items()},
            problems=problems or [],
            reply=(reply or "")[:2000] or None,
        )
    )


def flush() -> None:
    rows, _pending.rows = getattr(_pending, "rows", []), []
    if not rows:
        return
    try:
        from backend.app.vendor_onboarding.models import AiChatTurn
        from core.db import get_session

        with get_session() as session:
            for row in rows:
                session.add(AiChatTurn(**row))
    except Exception:  # noqa: BLE001 -- learning must never break a reply
        logger.exception("Could not store %d AI chat turn(s).", len(rows))


# ------------------------------------------------------------------ prompts


def lessons_for_prompt(applies_to: str) -> str:
    try:
        from backend.app.vendor_onboarding.models import AiLesson
        from core.db import get_session

        with get_session() as session:
            rows = session.execute(
                select(AiLesson.text)
                .where(AiLesson.active.is_(True), AiLesson.applies_to.in_([applies_to, "all"]))
                .order_by(AiLesson.id.desc())
                .limit(MAX_LESSONS_IN_PROMPT)
            ).scalars().all()
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    return "\nLessons learned from earlier chats (follow them):\n" + "\n".join(f"- {text}" for text in rows)


def examples_for_prompt() -> str:
    """Recent real messages that were understood AND fully accepted, with
    more than a one-word answer -- the phrasings worth copying."""
    try:
        from backend.app.vendor_onboarding.models import AiChatTurn
        from core.db import get_session

        with get_session() as session:
            rows = session.execute(
                select(AiChatTurn)
                .where(AiChatTurn.flow == "register")
                .order_by(AiChatTurn.id.desc())
                .limit(200)
            ).scalars().all()
            picked, seen = [], set()
            for row in rows:
                if row.problems or not row.fields or len((row.user_text or "").split()) < 3:
                    continue
                # Only turns that answered what was asked -- a reply that dodged
                # the question ("ifsc pata nahi") is not an example to copy.
                if row.asked_field and row.asked_field != "summary" and row.asked_field not in row.fields:
                    continue
                shape = tuple(sorted(row.fields))
                if shape in seen:
                    continue
                seen.add(shape)
                fields = {k: ("<value>" if k in ("account_number", "mobile") and v not in (None, "__ACCEPT__") else v)
                          for k, v in row.fields.items()}
                picked.append(f'Message: "{row.user_text[:300]}" (asked: {row.asked_field}) -> fields: '
                              f"{json.dumps(fields, ensure_ascii=False)}")
                if len(picked) >= MAX_EXAMPLES_IN_PROMPT:
                    break
    except Exception:  # noqa: BLE001
        return ""
    if not picked:
        return ""
    return "\nReal examples that were understood correctly before:\n" + "\n".join(picked)


# ------------------------------------------------------------------ nightly review

_REVIEW_SYSTEM = (
    "You improve a WhatsApp assistant that registers auto-parts vendors for CarTrends (Delhi) and "
    "routes their files. Vendors write Hinglish/Hindi/English with typos. Below are today's chat turns "
    "that went wrong (a value was rejected, the same question was asked again, the user cancelled or "
    "went quiet). Find PATTERNS where the assistant misunderstood the user or explained badly, and "
    "write short, general, reusable lessons that would have prevented them.\n"
    "Reply with JSON only: {\"lessons\": [{\"text\": \"...\", \"applies_to\": \"understand|reply|route\"}]}\n"
    "Rules: at most %d lessons; each under 40 words; general (no phone numbers, account numbers or "
    "personal data); never a lesson that relaxes validation (GSTIN/PAN/IFSC rules stay strict) or "
    "promises prices/stock/approval; skip anything already covered by the existing lessons; return "
    "an empty list if nothing useful."
)


def _troubled_turns(session, since) -> list:
    from backend.app.vendor_onboarding.models import AiChatTurn

    turns = session.execute(
        select(AiChatTurn).where(AiChatTurn.created_at >= since).order_by(AiChatTurn.whatsapp_number, AiChatTurn.id)
    ).scalars().all()
    troubled, previous = [], {}
    for turn in turns:
        repeated = turn.asked_field and previous.get(turn.whatsapp_number) == turn.asked_field
        if turn.problems or repeated or turn.intent in ("cancel",):
            troubled.append(turn)
        previous[turn.whatsapp_number] = turn.asked_field
    return troubled


def _scrub(text: str) -> str:
    """Hide long digit runs (phones, accounts) before text goes to the AI."""
    return re.sub(r"\d{6,}", "<num>", text or "")


def daily_review() -> list[str]:
    """Review the last day's troubled chats and store new lessons. Returns
    the new lesson texts (also sent to the admins). Never raises."""
    from backend.app.ai import llm
    from backend.app.vendor_onboarding.models import AiChatTurn, AiLesson
    from core.db import get_session

    if not llm.available():
        return []
    try:
        with get_session() as session:
            since = now_ist_naive() - timedelta(days=1)
            troubled = _troubled_turns(session, since)
            existing = session.execute(select(AiLesson.text).where(AiLesson.active.is_(True))).scalars().all()
            sample = [
                {
                    "asked": t.asked_field,
                    "user": _scrub(t.user_text)[:300],
                    "understood": {k: _scrub(str(v)) for k, v in (t.fields or {}).items()},
                    "rejected": [p.get("problem") if isinstance(p, dict) else str(p) for p in (t.problems or [])],
                    "bot_reply": _scrub(t.reply or "")[:300],
                }
                for t in troubled[-60:]
            ]
            for row in session.execute(select(AiChatTurn).where(AiChatTurn.created_at >= since)).scalars():
                row.reviewed = True
        if not sample:
            logger.info("AI daily review: no troubled chats in the last day -- nothing to learn.")
            return []
        data, provider = llm.ask_json(
            _REVIEW_SYSTEM % MAX_NEW_LESSONS_PER_DAY,
            "Existing lessons:\n" + ("\n".join(f"- {t}" for t in existing) or "(none)")
            + "\n\nToday's troubled turns:\n" + json.dumps(sample, ensure_ascii=False),
            purpose="reply",
        )
        new = []
        known = {t.strip().lower() for t in existing}
        for item in (data or {}).get("lessons") or []:
            text = str((item or {}).get("text") or "").strip()
            applies = str((item or {}).get("applies_to") or "all").strip()
            if not text or text.lower() in known or re.search(r"\d{6,}", text):
                continue
            new.append((text[:300], applies if applies in ("understand", "reply", "route") else "all"))
        new = new[:MAX_NEW_LESSONS_PER_DAY]
        created = []
        with get_session() as session:
            for text, applies in new:
                lesson = AiLesson(text=text, applies_to=applies)
                session.add(lesson)
                session.flush()
                created.append(f"#{lesson.id} {text}")
        logger.info("AI daily review (%s): %d troubled turn(s) -> %d new lesson(s).", provider, len(sample), len(created))
        if created:
            _tell_admins(
                "🧠 The WhatsApp assistant learned today:\n" + "\n".join(created)
                + "\n\nReply 'lesson off <number>' to switch one off."
            )
        return created
    except Exception:  # noqa: BLE001
        logger.exception("AI daily review failed.")
        return []


def _tell_admins(text: str) -> None:
    from backend.app.integrations.whatsapp.config import whatsapp_settings
    from backend.app.integrations.whatsapp.outbound import send_reply_safe

    from backend.app.vendor_onboarding.config import vendor_onboarding_settings

    for number in dict.fromkeys(whatsapp_settings.admin_phone_numbers + vendor_onboarding_settings.learning_report_numbers):
        send_reply_safe(number, text)


_LESSON_CMD = re.compile(r"^\s*lesson\s+(off|on)\s+#?(\d+)\s*$", re.I)


def handle_lesson_command(sender: str, text: str | None) -> str | None:
    """Admins: 'lesson off 3' / 'lesson on 3' / 'lessons'. None = not ours."""
    from backend.app.integrations.whatsapp import daily_stock
    from backend.app.vendor_onboarding.models import AiLesson
    from core.db import get_session

    lowered = " ".join((text or "").lower().split())
    match = _LESSON_CMD.match(lowered)
    from backend.app.vendor_onboarding.config import vendor_onboarding_settings

    allowed = daily_stock.is_admin_sender(sender) or sender in vendor_onboarding_settings.learning_report_numbers
    if not (match or lowered in ("lessons", "lesson list")) or not allowed:
        return None
    with get_session() as session:
        if match:
            lesson = session.get(AiLesson, int(match.group(2)))
            if lesson is None:
                return f"There is no lesson #{match.group(2)}."
            lesson.active = match.group(1) == "on"
            return f"Lesson #{lesson.id} switched {'on' if lesson.active else 'off'}: {lesson.text}"
        rows = session.execute(select(AiLesson).order_by(AiLesson.id)).scalars().all()
        if not rows:
            return "No lessons learned yet — the first review runs tonight at 9 pm."
        return "Lessons the assistant follows:\n" + "\n".join(
            f"#{r.id} {'✅' if r.active else '⛔'} {r.text}" for r in rows[-30:]
        )
