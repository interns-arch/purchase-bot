"""The vendor-registration chat: one question per message, validated as the
vendor types, then a summary to confirm and submission for approval.

Every function takes the caller's session and RETURNS the replies for the
sender (the caller sends them), so the flow can be tested without WhatsApp.
Messages to anyone else (approvers) are sent by `approvals`."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.vendor_onboarding import gst_lookup
from backend.app.vendor_onboarding import models as m
from backend.app.vendor_onboarding.config import vendor_onboarding_settings
from backend.app.vendor_onboarding.questions import BY_KEY, QUESTIONS, Question, format_answer, index_of
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive as now_ist

logger = get_logger(__name__)

START_COMMANDS = {"new vendor", "register vendor", "vendor registration", "vendor register", "naya vendor"}
SUMMARY = "summary"
_YES = {"ok", "okay", "yes", "y", "haan", "ha", "han", "correct", "same", "theek", "thik"}
_SKIP = {"skip", "no", "na", "n/a", "none", "nil", "-"}
_CONFIRM = {"confirm", "submit", "done", "yes", "ok", "haan"}
_CANCEL = {"cancel", "stop", "exit", "quit"}


_START_WORDS = {
    "new", "naya", "nayi", "create", "creation", "created", "register", "registration",
    "registered", "onboard", "onboarding", "add", "open", "signup", "sign", "join", "banana", "banao",
}
_VENDOR_WORDS = {"vendor", "vender", "vendors", "supplier", "suppliers", "dealer", "distributor"}


def is_start_command(text: str | None) -> bool:
    """Any short message asking to register a vendor: "new vendor",
    "Vendor creation", "create vendor", "vendor registration", "register as
    supplier", "naya vendor banana" ... A vendor word plus a create/register
    word, in a short message (so a long chat sentence is never mistaken)."""
    normalised = " ".join((text or "").lower().split())
    if normalised in START_COMMANDS:
        return True
    words = set(re.findall(r"[a-z]+", normalised))
    return len(words) <= 8 and bool(words & _VENDOR_WORDS) and bool(words & _START_WORDS)


def request_code(request: m.VendorRegistrationRequest) -> str:
    return f"VR-{request.id}"


def open_draft(number: str, session: Session) -> m.VendorRegistrationRequest | None:
    """This number's form in progress, expiring it if it went quiet."""
    draft = session.execute(
        select(m.VendorRegistrationRequest)
        .where(
            m.VendorRegistrationRequest.whatsapp_number == number,
            m.VendorRegistrationRequest.status == m.REG_DRAFT,
        )
        .order_by(m.VendorRegistrationRequest.id.desc())
    ).scalars().first()
    if draft is None:
        return None
    last_activity = draft.updated_at or draft.created_at
    if last_activity and now_ist() - last_activity > timedelta(
        hours=vendor_onboarding_settings.draft_expiry_hours
    ):
        draft.status = m.REG_EXPIRED
        session.flush()
        logger.info("Vendor registration %s expired (no reply).", request_code(draft))
        return None
    return draft


def _pending_request(number: str, session: Session) -> m.VendorRegistrationRequest | None:
    return session.execute(
        select(m.VendorRegistrationRequest).where(
            m.VendorRegistrationRequest.whatsapp_number == number,
            m.VendorRegistrationRequest.status == m.REG_AWAITING_APPROVAL,
        )
    ).scalars().first()


def is_on_behalf(request: m.VendorRegistrationRequest) -> bool:
    """Filled by the purchase team for a vendor (not by the vendor itself)."""
    return bool((request.answers or {}).get("_on_behalf"))


def _context(request: m.VendorRegistrationRequest) -> dict:
    # On behalf of a vendor, the sender's own number is NOT the vendor's mobile.
    number = None if is_on_behalf(request) else request.whatsapp_number
    return {"gst": request.gst_data, "number": number}


def _prompt(request: m.VendorRegistrationRequest, question: Question) -> str:
    position = index_of(question.key) + 1
    lines = [f"({position}/{len(QUESTIONS)}) {question.prompt}"]
    if question.choices:
        lines.extend(f"{i}. {option}" for i, option in enumerate(question.choices(), start=1))
    suggestion = question.suggest(request.answers or {}, _context(request)) if question.suggest else None
    if suggestion:
        lines.append(f"\nReply OK to use: {suggestion}")
    if question.optional:
        lines.append("(Reply SKIP if not applicable)")
    return "\n".join(lines)


def summary_text(request: m.VendorRegistrationRequest) -> str:
    answers = request.answers or {}
    lines = ["Please check your details:"]
    for i, question in enumerate(QUESTIONS, start=1):
        lines.append(f"{i}. {question.label}: {format_answer(question.key, answers.get(question.key))}")
    gst = request.gst_data or {}
    if gst.get("status"):
        lines.append(f"\nGST portal: {gst.get('legal_name') or '-'} — {gst['status']}")
    lines.append("\nReply CONFIRM to submit, EDIT <number> to change an answer (e.g. EDIT 12), or CANCEL.")
    return "\n".join(lines)


def _next_step(request: m.VendorRegistrationRequest) -> str:
    """The first unanswered question after the current one, or SUMMARY."""
    answers = request.answers or {}
    start = index_of(request.current_step) + 1 if request.current_step in BY_KEY else 0
    for question in QUESTIONS[start:]:
        if question.key not in answers:
            return question.key
    return SUMMARY


def _advance(request: m.VendorRegistrationRequest) -> list[str]:
    if request.editing:
        request.editing = False
        request.current_step = SUMMARY
    else:
        request.current_step = _next_step(request)
    if request.current_step == SUMMARY:
        return [summary_text(request)]
    return [_prompt(request, BY_KEY[request.current_step])]


def start(number: str, session: Session, trigger_text: str = "") -> list[str]:
    from backend.app.integrations.whatsapp import registry

    staff = vendor_onboarding_settings.is_purchase_team(number)
    if not staff:
        pending = _pending_request(number, session)
        if pending is not None:
            return [
                f"Your registration {request_code(pending)} for {pending.vendor_name} is waiting "
                "for approval. You'll get a message here as soon as it is decided."
            ]
        known = registry.lookup(number, session)
        if known is not None and known.party_type == "vendor":
            return [f"This number is already registered as vendor {known.name}. No new registration is needed."]

    draft = open_draft(number, session)
    if draft is not None:
        question = BY_KEY.get(draft.current_step or "")
        resume = summary_text(draft) if draft.current_step == SUMMARY else _prompt(draft, question or QUESTIONS[0])
        return ["Continuing your vendor registration.", resume]

    answers = {}
    if staff:
        answers = {"_on_behalf": True, "_created_by": vendor_onboarding_settings.name_of(number)}
    draft = m.VendorRegistrationRequest(whatsapp_number=number, answers=answers, current_step=QUESTIONS[0].key)
    session.add(draft)
    session.flush()
    logger.info("Vendor registration %s started by %s%s.", request_code(draft), number, " (on behalf)" if staff else "")
    if staff:
        tell = (
            f"greet {vendor_onboarding_settings.name_of(number)} from the purchase team by name; they are "
            "registering a NEW VENDOR on the vendor's behalf, so every answer is about that vendor (and "
            "the mobile must be the VENDOR's own number). They can paste all details in one message. "
            + ("Because they are an approver, the vendor will be created as soon as they confirm — no "
               "approval needed." if vendor_onboarding_settings.is_approver(number)
               else "After they confirm it goes to Prateek sir / NK Jain sir for approval.")
        )
    else:
        tell = (
            "welcome them; they can answer in their own words and language, give several details in "
            "one message, ask anything, and type CANCEL to stop; it takes about 5 minutes"
        )
    welcome = _ai_reply(
        draft,
        {
            "event": "vendor registration just started",
            "tell_them": tell,
            "next_question": _question_facts(draft, QUESTIONS[0].key),
        },
        trigger_text or "new vendor",
    )
    if welcome:
        return [welcome]
    return [
        "Welcome to CarTrends vendor registration! I'll ask a few questions one by one "
        f"({len(QUESTIONS)} in all, most take a word or two). Reply CANCEL any time to stop.",
        _prompt(draft, QUESTIONS[0]),
    ]


def _store(request: m.VendorRegistrationRequest, key: str, value) -> None:
    answers = dict(request.answers or {})
    answers[key] = value
    request.answers = answers  # reassign so the JSON column is marked dirty
    if key == "vendor_name":
        request.vendor_name = value
    if key == "gstin":
        request.gstin = value


def _check_gstin(request: m.VendorRegistrationRequest, gstin: str, session: Session) -> str | None:
    """Duplicate + GST-portal checks. Returns an error for the vendor, or None."""
    duplicate = session.execute(
        select(m.VendorRegistrationRequest).where(
            m.VendorRegistrationRequest.gstin == gstin,
            m.VendorRegistrationRequest.id != request.id,
            m.VendorRegistrationRequest.status.in_([m.REG_AWAITING_APPROVAL, m.REG_CREATED]),
        )
    ).scalars().first()
    if duplicate is not None:
        state = "already registered" if duplicate.status == m.REG_CREATED else "already waiting for approval"
        return f"GSTIN {gstin} is {state} ({request_code(duplicate)}, {duplicate.vendor_name}). Please contact the purchase team."

    record = gst_lookup.lookup(gstin)
    if record is None:
        request.gst_data = None
        return None
    request.gst_data = {
        "legal_name": record.legal_name,
        "trade_name": record.trade_name,
        "status": record.status,
        "address": record.address,
        "pincode": record.pincode,
        "registration_date": record.raw.get("registration_date"),
        "taxpayer_type": record.raw.get("taxpayer_type"),
    }
    if not record.is_active:
        return f"GSTIN {gstin} is '{record.status}' on the GST portal, so it can't be registered. Please send an active GSTIN."
    return None


def answer(number: str, text: str, session: Session) -> list[str] | None:
    """Handle one message from a number with a form in progress. Returns the
    replies, or None when this number has no form open (not ours)."""
    request = open_draft(number, session)
    if request is None:
        return None
    reply = (text or "").strip()
    lowered = " ".join(reply.lower().split())

    if lowered in _CANCEL:
        request.status = m.REG_CANCELLED
        session.flush()
        return ["Registration cancelled. Send NEW VENDOR any time to start again."]

    ai_replies = _answer_with_ai(request, reply, lowered, session)
    if ai_replies is not None:
        return ai_replies

    if request.current_step == SUMMARY:
        return _answer_summary(request, lowered, session)

    question = BY_KEY.get(request.current_step or "")
    if question is None:  # defensive: unknown step -> restart at the first gap
        request.current_step = None
        return _advance(request)

    if lowered in _YES and question.suggest:
        suggestion = question.suggest(request.answers or {}, _context(request))
        if suggestion:
            reply = suggestion
    if lowered in _SKIP and question.optional:
        _store(request, question.key, None)
        return _advance(request)

    value, error = question.validate(reply, request.answers or {})
    if error:
        return [error]
    if question.key == "gstin":
        problem = _check_gstin(request, value, session)
        if problem:
            return [problem]
        # A changed GSTIN invalidates the GST-derived answers.
        for key in ("vendor_name", "pan", "address"):
            if key in (request.answers or {}) and not request.editing:
                answers = dict(request.answers)
                answers.pop(key)
                request.answers = answers
    _store(request, question.key, value)
    session.flush()
    replies = []
    if question.key == "gstin" and request.gst_data:
        replies.append(f"✓ GSTIN verified: {request.gst_data.get('legal_name')} ({request.gst_data.get('status')})")
    return replies + _advance(request)


def _answer_summary(request: m.VendorRegistrationRequest, lowered: str, session: Session) -> list[str]:
    if lowered.startswith("edit"):
        number = lowered[4:].strip()
        if number.isdigit() and 1 <= int(number) <= len(QUESTIONS):
            question = QUESTIONS[int(number) - 1]
            request.current_step = question.key
            request.editing = True
            session.flush()
            return [_prompt(request, question)]
        return [f"Reply EDIT followed by a number from 1 to {len(QUESTIONS)}, e.g. EDIT 12."]
    if lowered in _CONFIRM:
        return submit(request, session)
    return ["Reply CONFIRM to submit, EDIT <number> to change an answer, or CANCEL."]


def submit(request: m.VendorRegistrationRequest, session: Session) -> list[str]:
    from backend.app.vendor_onboarding import approvals

    missing = [q.label for q in QUESTIONS if not q.optional and not (request.answers or {}).get(q.key)]
    if missing:
        request.current_step = None
        return [f"Some details are still missing: {', '.join(missing)}."] + _advance(request)
    request.status = m.REG_AWAITING_APPROVAL
    request.current_step = None
    request.submitted_at = now_ist()
    session.flush()
    if is_on_behalf(request) and vendor_onboarding_settings.is_approver(request.whatsapp_number):
        # Created by an approver himself: no approval round.
        return approvals.auto_approve(request, session)
    approvals.request_vendor_approval(request, session)
    logger.info("Vendor registration %s submitted for approval.", request_code(request))
    if is_on_behalf(request):
        return [
            f"Done — {request_code(request)} ({request.vendor_name}) has been sent to Prateek sir / "
            "NK Jain sir for approval. I'll tell you here once it is decided."
        ]
    return [
        f"Thank you! Your registration {request_code(request)} has been sent for approval. "
        "You'll get a message here once it is approved."
    ]


# --------------------------------------------------------------------------
# Natural-language path (Gemini): see `ai_chat` for the rules. The AI only
# reads and writes language; every value still goes through the validators.
# --------------------------------------------------------------------------

_EDIT_NUMBER = re.compile(r"^edit\s*(\d+)$")
_ACCEPT = "__ACCEPT__"


def _first_gap(request: m.VendorRegistrationRequest) -> str:
    answers = request.answers or {}
    for question in QUESTIONS:
        if question.key not in answers:
            return question.key
    return SUMMARY


def _question_facts(request: m.VendorRegistrationRequest, key: str | None) -> dict | None:
    question = BY_KEY.get(key or "")
    if question is None:
        return None
    facts: dict = {"field": question.label, "ask": question.prompt, "optional": question.optional}
    if question.choices:
        facts["choices"] = question.choices()
    suggestion = question.suggest(request.answers or {}, _context(request)) if question.suggest else None
    if suggestion:
        facts["suggestion"] = suggestion
    return facts


def _suggestions(request: m.VendorRegistrationRequest) -> dict:
    """Every pre-filled value the vendor could accept with "ok"/"same"."""
    result = {}
    for question in QUESTIONS:
        if question.suggest and question.key not in (request.answers or {}):
            value = question.suggest(request.answers or {}, _context(request))
            if value:
                result[question.key] = value
    return result


def _apply_value(request: m.VendorRegistrationRequest, key: str, raw, session: Session) -> str | None:
    """Validate and store one field. Returns a problem for the vendor, or None."""
    question = BY_KEY[key]
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in _SKIP):
        if question.optional:
            _store(request, key, None)
            return None
        return f"{question.label} is required."
    if raw == _ACCEPT:
        raw = question.suggest(request.answers or {}, _context(request)) if question.suggest else None
        if not raw:
            return f"Please type the {question.label}."
    value, error = question.validate(str(raw), request.answers or {})
    if error:
        return error
    if key == "gstin":
        previous = (request.answers or {}).get("gstin")
        problem = _check_gstin(request, value, session)
        if problem:
            return problem
        if previous and previous != value:
            # A different GSTIN invalidates the answers that came from the old one.
            answers = dict(request.answers)
            for derived in ("vendor_name", "pan", "address"):
                answers.pop(derived, None)
            request.answers = answers
    _store(request, key, value)
    return None


def _ai_reply(request: m.VendorRegistrationRequest, facts: dict, user_text: str) -> str | None:
    from backend.app.vendor_onboarding import ai_chat

    if not ai_chat.available():
        return None
    if is_on_behalf(request):
        name = vendor_onboarding_settings.honorific(request.whatsapp_number) or (request.answers or {}).get("_created_by")
        facts = {
            "talking_to": f"{name} ({vendor_onboarding_settings.role_of(request.whatsapp_number) or 'CarTrends purchase team'}), "
            "who is registering a vendor on the vendor's behalf — greet/address them exactly as "
            f"'{name}'",
            **facts,
        }
    else:
        # A vendor registering itself: by name once we know who they are.
        person = ai_chat.who_is(request.whatsapp_number)
        contact = (request.answers or {}).get("contact_person")
        name = (person or {}).get("name") or (f"{contact} ji" if contact and not contact.lower().endswith("ji") else contact)
        if name:
            facts = {"talking_to": f"{name} (the vendor, registering itself) — address them exactly as '{name}'", **facts}
    reply = ai_chat.compose(facts, user_message=user_text, chat_history=ai_chat.history(request.whatsapp_number))
    if reply:
        ai_chat.remember(request.whatsapp_number, "in", user_text)
        ai_chat.remember(request.whatsapp_number, "out", reply)
    return reply


def _answer_with_ai(
    request: m.VendorRegistrationRequest, text: str, lowered: str, session: Session
) -> list[str] | None:
    """The natural-language turn, or None to use the fixed flow (AI down)."""
    from backend.app.vendor_onboarding import ai_chat

    if not ai_chat.available():
        return None
    current = request.current_step
    question = BY_KEY.get(current or "")
    intent, edit_field, user_question = "answer", None, None

    # Unambiguous one-word replies need no AI to understand.
    edit_number = _EDIT_NUMBER.match(lowered)
    if question is not None and lowered in _YES and question.suggest:
        updates = {current: _ACCEPT}
    elif question is not None and lowered in _SKIP and question.optional:
        updates = {current: None}
    elif question is not None and question.choices and lowered.isdigit():
        updates = {current: text}
    elif current == SUMMARY and lowered in _CONFIRM:
        updates, intent = {}, "confirm"
    elif current == SUMMARY and edit_number and 1 <= int(edit_number.group(1)) <= len(QUESTIONS):
        updates, intent, edit_field = {}, "edit", QUESTIONS[int(edit_number.group(1)) - 1].key
    else:
        understood = ai_chat.understand(
            text,
            current_key=None if current == SUMMARY else current,
            answers=request.answers or {},
            chat_history=ai_chat.history(request.whatsapp_number),
            suggestions=_suggestions(request),
        )
        if understood is None:
            return None
        updates = understood["fields"]
        intent, edit_field, user_question = understood["intent"], understood["edit_field"], understood["question"]
        logger.info("%s AI read %r -> fields=%s intent=%s", request_code(request), text[:80], list(updates), intent)

    if intent == "cancel" and not updates:
        request.status = m.REG_CANCELLED
        session.flush()
        cancelled = _ai_reply(
            request,
            {"event": "registration cancelled at the user's request",
             "tell_them": "they can start again any time by saying new vendor"},
            text,
        )
        return [cancelled or "Registration cancelled. Send NEW VENDOR any time to start again."]

    saved, skipped, problems = [], [], []
    had_gst = bool(request.gst_data)
    for key in sorted(updates, key=index_of):
        problem = _apply_value(request, key, updates[key], session)
        if problem:
            problems.append({"key": key, "field": BY_KEY[key].label, "problem": problem})
        elif (request.answers or {}).get(key) is None:
            skipped.append(BY_KEY[key].label)
        else:
            saved.append(BY_KEY[key].label)
    session.flush()

    if current == SUMMARY and intent == "confirm" and not updates:
        replies = submit(request, session)
        if request.status == m.REG_AWAITING_APPROVAL and not is_on_behalf(request):
            thanks = _ai_reply(
                request,
                {"event": "registration submitted for approval", "reference": request_code(request),
                 "tell_them": "thank them; our approvers will review it and they will get a message here "
                 "with their vendor code once approved"},
                text,
            )
            if thanks:
                return [thanks]
        return replies

    if intent == "edit" and edit_field and not updates:
        request.current_step = edit_field
    elif problems:
        request.current_step = problems[0]["key"]
    else:
        request.current_step = _first_gap(request)
    request.editing = False
    session.flush()

    show_summary = request.current_step == SUMMARY
    facts: dict = {
        "saved": saved,
        "skipped": skipped,
        "problems": [{"field": p["field"], "problem": p["problem"]} for p in problems],
        "user_question": user_question,
        "next_question": None if show_summary else _question_facts(request, request.current_step),
        "show_summary": show_summary,
        "questions_left": sum(1 for q in QUESTIONS if q.key not in (request.answers or {})),
    }
    if request.gst_data and not had_gst:
        facts["gst_verified_on_portal"] = {
            "legal_name": request.gst_data.get("legal_name"),
            "status": request.gst_data.get("status"),
        }
    reply = _ai_reply(request, facts, text)
    from backend.app.vendor_onboarding import learning

    learning.log_turn(
        request.whatsapp_number,
        "register",
        text,
        asked_field=current,
        intent=intent,
        fields=updates,
        problems=[{"field": p["field"], "problem": p["problem"]} for p in problems],
        reply=reply,
    )
    if reply is None:  # AI went quiet mid-turn: say the same thing in fixed words
        lines = [p["problem"] for p in problems]
        if show_summary:
            return lines + [summary_text(request)]
        return lines + [_prompt(request, BY_KEY[request.current_step])]
    return [reply, summary_text(request)] if show_summary else [reply]
