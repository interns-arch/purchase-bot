"""WhatsApp decisions: vendor approvals (Prateek sir / NK Jain sir) and ledger
checks (Sunny sir / Anurag).

Every request goes to ALL listed people; the FIRST reply decides and the
others are told. The row is locked (SELECT ... FOR UPDATE on Postgres) while
a reply is applied, so two people answering at the same moment can never
both win.

Replies:  APPROVE VR-12 | REJECT VR-12 <reason> | PASS LG-7 | FAIL LG-7 <reason>"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.outbound import send_document_safe, send_reply_safe
from backend.app.notifications import broker
from backend.app.vendor_onboarding import models as m
from backend.app.vendor_onboarding.config import vendor_onboarding_settings as settings
from backend.app.vendor_onboarding.questions import QUESTIONS, format_answer
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive as now_ist

logger = get_logger(__name__)

_REPLY = re.compile(
    r"^\s*(?P<verb>approve|approved|reject|rejected|pass|passed|fail|failed)\s*[:\-]?\s*"
    r"(?P<kind>vr|lg)\s*[-\s]?\s*(?P<id>\d+)\b[\s:\-,.]*(?P<reason>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def notify(numbers: list[str], text: str, *, template: str = "", params: list[str] | None = None) -> None:
    """Send `text` to each number. With a template configured, the template
    goes first (it reaches people outside the 24-hour window and reopens it),
    then the full text. Never raises."""
    for number in numbers:
        if template:
            try:
                WhatsAppClient(whatsapp_settings).send_template_message(
                    number, template, settings.template_language, params or []
                )
            except Exception:  # noqa: BLE001 -- fall through to the plain text
                logger.exception("Template %s to %s failed.", template, number)
        # Every message to a known person opens with their name.
        name = settings.honorific(number)
        send_reply_safe(number, f"Namaste {name},\n{text}" if name else text)


def _publish(level: str, title: str, message: str) -> None:
    try:
        broker.publish(level, title, message)
    except Exception:  # noqa: BLE001 -- the UI toast is best-effort
        logger.exception("Could not publish '%s'.", title)


# --------------------------------------------------------------------------
# Vendor registration
# --------------------------------------------------------------------------


def _v(answers: dict, key: str) -> str | None:
    value = answers.get(key)
    if value in (None, "", []):
        return None
    return format_answer(key, value)


def _section(title: str, rows: list[tuple[str, str | None]]) -> list[str]:
    rows = [(label, value) for label, value in rows if value]
    if not rows:
        return []
    return ["", f"*{title}*", *(f"• {label}: {value}" for label, value in rows)]


def vendor_summary(request: m.VendorRegistrationRequest, *, heading: str | None = None) -> str:
    """Every detail of the vendor, grouped so an approver can check it at a
    glance on a phone (12 Nov 2026: "well formatted and easy to understand")."""
    a = request.answers or {}
    gst = request.gst_data or {}
    if gst:
        gst_line = f"✅ {gst.get('status') or 'checked'} on GST portal"
        if gst.get("legal_name"):
            gst_line += f" — {gst['legal_name']}"
        if gst.get("registration_date"):
            gst_line += f" (since {gst['registration_date']})"
    else:
        gst_line = "⚠️ not verified online (format and check digit OK)"
    credit = ", ".join(x for x in (
        f"{a['credit_days']} days" if a.get("credit_days") else None,
        f"limit ₹{a['credit_limit']}" if a.get("credit_limit") else None,
    ) if x) or None
    lines = [heading or f"🆕 *New vendor for approval — {_code(request)}*", f"*{a.get('vendor_name') or request.vendor_name or '-'}*"]
    lines += _section("🏢 Company", [
        ("GSTIN", _v(a, "gstin")),
        ("GST check", gst_line),
        ("PAN", _v(a, "pan")),
        ("Vendor type", _v(a, "vendor_type")),
        ("Address", _v(a, "address")),
    ])
    lines += _section("📞 Contact", [
        ("Person", _v(a, "contact_person")),
        ("Mobile", _v(a, "mobile")),
        ("Email", _v(a, "email")),
    ])
    lines += _section("🚗 Brands", [
        ("Supplies", _v(a, "brands_supplied")),
        ("Discount", _v(a, "brand_discounts")),
    ])
    lines += _section("🏦 Bank", [
        ("Account holder", _v(a, "account_holder")),
        ("Bank", _v(a, "bank_name")),
        ("Account no.", _v(a, "account_number")),
        ("IFSC", _v(a, "ifsc")),
    ])
    lines += _section("📋 Terms", [
        ("Credit", credit),
        ("Dispatch", _v(a, "dispatch_mode")),
        ("Freight", _v(a, "freight_terms")),
        ("Transporter", _v(a, "transporter")),
        ("Lead time", f"{a['lead_time_days']} days" if a.get("lead_time_days") else None),
        ("Min. order", f"₹{a['min_order_value']}" if a.get("min_order_value") else None),
        ("Returns", _v(a, "return_policy")),
    ])
    lines += _section("🖥️ Dealer Portal", [
        ("Also add as customer", _v(a, "also_customer") or "Nahi (not asked)"),
    ])
    if a.get("remarks"):
        lines += _section("📝 Remarks", [("Note", _v(a, "remarks"))])
    if a.get("_on_behalf"):
        filled = f"{a.get('_created_by') or request.whatsapp_number} (purchase team) for the vendor"
    else:
        filled = f"the vendor itself, from WhatsApp {request.whatsapp_number}"
    lines += ["", f"👤 Filled by {filled}"]
    return "\n".join(lines)


def _code(request: m.VendorRegistrationRequest) -> str:
    return f"VR-{request.id}"


_BUTTON = re.compile(r"^\s*VRBTN\s+(?P<verb>APPROVE|REJECT)\s+(?P<id>\d+)\s*$", re.IGNORECASE)


def _buttons(row_id: int) -> list[tuple[str, str]]:
    return [(f"VRBTN APPROVE {row_id}", "✅ Approve"), (f"VRBTN REJECT {row_id}", "❌ Reject")]


def _reminder_line(request: m.VendorRegistrationRequest) -> str:
    since = request.submitted_at or request.created_at
    when = f" since {since:%d %b, %I:%M %p}" if since else ""
    return f"🔔 *Reminder* — this vendor is still waiting for your approval{when}."


# Numbers that have SEEN the full card for a request (inside the 24-hour
# window). Outside it only the short approval template gets through -- the
# details that follow it are blocked by WhatsApp (131047 / 131049, VR-26 on
# 20 Nov 2026) -- so the card is sent again the moment he writes to the bot.
_CARD_KEY = "_card_to"


def _mark_card(request: m.VendorRegistrationRequest, number: str) -> None:
    answers = dict(request.answers or {})
    seen = list(answers.get(_CARD_KEY) or [])
    if number not in seen:
        seen.append(number)
        answers[_CARD_KEY] = seen
        request.answers = answers  # reassign so the JSON column is marked dirty


def send_approval_card(
    number: str, request: m.VendorRegistrationRequest, warning: str | None = None, *, reminder: bool = False,
    in_window: bool | None = None,
) -> bool:
    """The vendor's details with APPROVE / REJECT buttons. Buttons need the
    24-hour window (and the Meta number): outside it the approved template
    reaches him first and the details follow as text with the typed replies,
    exactly as before. `in_window=True`: he has just written to the bot.
    True when the full card (details + buttons) went out. Never raises."""
    from backend.app.integrations.whatsapp import outbound

    code = _code(request)
    name = settings.honorific(number)
    details = vendor_summary(request) + (f"\n\n{warning}" if warning else "")
    if reminder:
        details = _reminder_line(request) + "\n\n" + details
    typed = f"Ya likhiye: APPROVE {code} / REJECT {code}"
    if in_window if in_window is not None else outbound.in_service_window(number):
        body = (f"Namaste {name},\n" if name else "") + details
        try:
            client = WhatsAppClient(whatsapp_settings)
            if len(body) <= 1024:
                client.send_button_message(number, body, _buttons(request.id), footer=typed)
            else:  # too long for one card: the details, then a short card
                send_reply_safe(number, body)
                client.send_button_message(
                    number, f"{code} — *{request.vendor_name}*\nApprove karein?", _buttons(request.id), footer=typed
                )
            _mark_card(request, number)
            return True
        except Exception:  # noqa: BLE001 -- fall back to text below
            logger.exception("%s: approval buttons to %s failed -- sending text.", code, number)
    notify(
        [number],
        details + f"\n\nReply APPROVE {code} or REJECT {code} <reason>.",
        template=settings.approval_template,
        params=[code, f"{request.vendor_name} ({request.gstin})"],
    )
    return False


def _duplicate_warning(request: m.VendorRegistrationRequest, session: Session) -> str | None:
    from core.services import vendor_service

    similar = vendor_service.get_vendor_by_name(request.vendor_name or "", session)
    if similar is None:
        return None
    return f"⚠️ A vendor named '{similar.name}' already exists in ProcureHub — check it is not a duplicate."


# "hi" / "details" from an approver who has a card waiting: the card IS the answer.
_HELLO = re.compile(
    r"^\s*(hi+|hello+|helo|hey|namaste|namaskar|good\s*(morning|afternoon|evening)|ok|okay|ji|haan|"
    r"details?|show|dikhao|bhejo|batao)\b[\s!.?ji]*$",
    re.IGNORECASE,
)


def deliver_waiting_cards(sender: str, session: Session) -> int:
    """An approver just wrote to the bot, so his 24-hour window is open: send
    him the full card (details + buttons) of every vendor still waiting that
    he has not seen yet. Returns how many were sent. Never raises."""
    if sender not in settings.approver_numbers:
        return 0
    try:
        waiting = session.execute(
            select(m.VendorRegistrationRequest)
            .where(m.VendorRegistrationRequest.status == m.REG_AWAITING_APPROVAL)
            .order_by(m.VendorRegistrationRequest.id)
        ).scalars().all()
        sent = 0
        for request in waiting:
            if sender in ((request.answers or {}).get(_CARD_KEY) or []):
                continue
            if send_approval_card(sender, request, _duplicate_warning(request, session), in_window=True):
                sent += 1
        if sent:
            session.flush()
            logger.info("Approver %s wrote to the bot: %d waiting vendor card(s) sent.", sender, sent)
        return sent
    except Exception:  # noqa: BLE001 -- the normal reply still follows
        logger.exception("Could not send the waiting vendor cards to %s.", sender)
        return 0


def request_vendor_approval(request: m.VendorRegistrationRequest, session: Session, *, reminder: bool = False) -> None:
    code = _code(request)
    warning = _duplicate_warning(request, session)
    if not settings.approver_numbers:
        logger.warning("%s submitted but VENDOR_APPROVER_NUMBERS is empty -- nobody was asked.", code)
    for number in settings.approver_numbers:
        send_approval_card(number, request, warning, reminder=reminder)
    _publish("info", f"Vendor registration {code}", f"{request.vendor_name} is waiting for approval.")


# Reject tapped -> the approver's NEXT message is the reason (or SKIP).
# In memory on purpose: lost on a restart, he can still type REJECT VR-n.
_REASON_WAIT_SECONDS = 15 * 60
_awaiting_reason: dict[str, tuple[int, float]] = {}


def _button_tap(sender: str, verb: str, row_id: int, session: Session) -> list[str]:
    import time

    if verb == "approve":
        _awaiting_reason.pop(sender, None)
        return _decide_vendor(sender, "approve", row_id, "", session)
    if sender not in settings.approver_numbers:
        return ["Only the vendor approvers can approve or reject vendor registrations."]
    request = session.get(m.VendorRegistrationRequest, row_id)
    if request is None:
        return [f"There is no vendor registration VR-{row_id}."]
    if request.status != m.REG_AWAITING_APPROVAL:
        who = request.decided_by or "someone"
        return [f"VR-{row_id} ({request.vendor_name}) was already decided by {who}: {request.status.replace('_', ' ')}."]
    _awaiting_reason[sender] = (row_id, time.monotonic())
    return [f"❌ VR-{row_id} ({request.vendor_name}) reject karne ka *reason* likhiye — vendor ko bheja jayega.\n"
            "Bina reason ke reject: *SKIP*\nGalti se dabaya? *CANCEL* likhiye."]


def _reason_reply(sender: str, text: str | None, session: Session) -> list[str] | None:
    import time

    waiting = _awaiting_reason.get(sender)
    if not waiting:
        return None
    row_id, at = waiting
    _awaiting_reason.pop(sender, None)
    if time.monotonic() - at > _REASON_WAIT_SECONDS:
        return None
    if parse_reply(text) or _BUTTON.match(text or ""):
        return None  # a fresh decision: handled normally
    reason = " ".join((text or "").split())
    if reason.lower() in {"cancel", "galti", "no", "nahi"}:
        return [f"Theek hai, VR-{row_id} reject nahi kiya. Abhi bhi approval ke liye ruka hai."]
    if reason.lower() in {"skip", "-", "no reason"}:
        reason = ""
    return _decide_vendor(sender, "reject", row_id, reason, session)


def _locked(model, row_id: int, session: Session):
    return session.execute(select(model).where(model.id == row_id).with_for_update()).scalar_one_or_none()


def _decide_vendor(sender: str, verb: str, row_id: int, reason: str, session: Session) -> list[str]:
    from backend.app.vendor_onboarding import service

    if sender not in settings.approver_numbers:
        return ["Only the vendor approvers can approve or reject vendor registrations."]
    request = _locked(m.VendorRegistrationRequest, row_id, session)
    if request is None:
        return [f"There is no vendor registration VR-{row_id}."]
    code = _code(request)
    if request.status == m.REG_DRAFT:
        return [f"{code} is still being filled in by the vendor — nothing to approve yet."]
    if request.status != m.REG_AWAITING_APPROVAL:
        who = request.decided_by or "someone"
        return [f"{code} ({request.vendor_name}) was already decided by {who}: {request.status.replace('_', ' ')}."]

    name = settings.name_of(sender)
    request.decided_by = name
    request.decided_at = now_ist()
    others = [n for n in settings.approver_numbers if n != sender]

    if verb.startswith("reject"):
        request.status = m.REG_REJECTED
        request.reason = reason or None
        session.flush()
        if (request.answers or {}).get("_on_behalf"):
            send_reply_safe(
                request.whatsapp_number,
                f"Vendor registration {code} ({request.vendor_name}) was rejected by {name}."
                + (f"\nReason: {reason}" if reason else ""),
            )
        else:
            send_reply_safe(
                request.whatsapp_number,
                f"Sorry, your vendor registration {code} was not approved."
                + (f"\nReason: {reason}" if reason else "")
                + "\nYou can send NEW VENDOR to apply again.",
            )
        notify(others, f"{code} ({request.vendor_name}) was REJECTED by {name}." + (f" Reason: {reason}" if reason else ""))
        _publish("warning", f"Vendor {code} rejected", f"{request.vendor_name} — by {name}")
        return [f"Rejected {code} ({request.vendor_name}). The vendor has been told."]

    ok, detail = service.create_vendor_from_request(request, session)
    if not ok:
        notify(others, f"{code} ({request.vendor_name}) was APPROVED by {name}, but creating it failed: {detail}")
        _publish("error", f"Vendor {code} creation failed", detail)
        return [f"Approved {code}, but creating the vendor failed: {detail}\nIt can be retried from the Vendor Onboarding page."]
    notify(others, f"{code} ({request.vendor_name}) was APPROVED by {name}. {detail}")
    _publish("success", f"Vendor {code} created", f"{request.vendor_name} — approved by {name}")
    return [f"Approved {code} ({request.vendor_name}). {detail}"]


def auto_approve(request: m.VendorRegistrationRequest, session: Session) -> list[str]:
    """An approver registered the vendor himself: create it straight away and
    tell the other approvers. Returns the replies for the approver."""
    from backend.app.vendor_onboarding import service

    code = _code(request)
    name = settings.name_of(request.whatsapp_number)
    request.decided_by = f"{name} (created by approver)"
    request.decided_at = now_ist()
    session.flush()
    others = [n for n in settings.approver_numbers if n != request.whatsapp_number]
    ok, detail = service.create_vendor_from_request(request, session)
    if not ok:
        _publish("error", f"Vendor {code} creation failed", detail)
        return [f"{code} ({request.vendor_name}): creating the vendor failed — {detail}"]
    notify(others, f"FYI: {name} created vendor {request.vendor_name} ({code}) directly. {detail}")
    _publish("success", f"Vendor {code} created", f"{request.vendor_name} — by {name}")
    return [f"Vendor {request.vendor_name} created ({code}), no approval needed. {detail}"]


# --------------------------------------------------------------------------
# Ledgers
# --------------------------------------------------------------------------


def request_ledger_check(submission: m.VendorLedgerSubmission, summary: str, session: Session) -> None:
    from pathlib import Path

    code = f"LG-{submission.id}"
    text = f"{summary}\n\nReply PASS {code} or FAIL {code} <reason>."
    if not settings.ledger_checker_numbers:
        logger.warning("%s ready but LEDGER_CHECKER_NUMBERS is empty -- nobody was asked.", code)
    notify(
        settings.ledger_checker_numbers,
        text,
        template=settings.ledger_template,
        params=[code, (summary.splitlines() or [""])[0][:200]],
    )
    path = Path(submission.file_path)
    if path.exists():
        import mimetypes

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        content = path.read_bytes()
        for number in settings.ledger_checker_numbers:
            send_document_safe(number, content, submission.original_filename or path.name, mime, caption=code)
    _publish("info", f"Ledger {code}", "Waiting for the accounts check.")


def _decide_ledger(sender: str, verb: str, row_id: int, reason: str, session: Session) -> list[str]:
    from backend.app.vendor_onboarding import ledger

    if sender not in settings.ledger_checker_numbers:
        return ["Only the accounts team can pass or fail ledgers."]
    submission = _locked(m.VendorLedgerSubmission, row_id, session)
    if submission is None:
        return [f"There is no ledger LG-{row_id}."]
    code = f"LG-{submission.id}"
    if submission.status != m.LG_AWAITING_ACCOUNTS:
        who = submission.decided_by or "someone"
        return [f"{code} was already decided by {who}: {submission.status.replace('_', ' ')}."]
    if verb.startswith("fail") and not reason:
        return [f"Please add the reason so the vendor knows what to fix: FAIL {code} <reason>"]

    name = settings.name_of(sender)
    submission.decided_by = name
    submission.decided_at = now_ist()
    others = [n for n in settings.ledger_checker_numbers if n != sender]
    if verb.startswith("fail"):
        message = ledger.on_fail(submission, name, reason, session)
    else:
        message = ledger.on_pass(submission, name, session)
    notify(others, f"{code} was {'FAILED' if verb.startswith('fail') else 'PASSED'} by {name}." + (f" Reason: {reason}" if reason else ""))
    return [message]


# --------------------------------------------------------------------------


def parse_reply(text: str | None) -> tuple[str, str, int, str] | None:
    match = _REPLY.match(text or "")
    if not match:
        return None
    return (
        match.group("verb").lower(),
        match.group("kind").lower(),
        int(match.group("id")),
        " ".join(match.group("reason").split()),
    )


# An approver's decision WITHOUT the code -- "Approved", "ok approve", "reject
# price list nahi hai" (NK Jain sir, 30 Oct 2026: his "Approved" for VR-15 was
# not taken and the chat assistant answered as if it had been).
_BARE = re.compile(
    r"^\s*(?:ok\s+|yes\s+|haan\s+)?(?P<verb>approve|approved|reject|rejected)\b[\s:\-,.!]*(?P<reason>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _bare_decision(sender: str, text: str | None, session: Session) -> list[str] | None:
    match = _BARE.match(text or "")
    if not match or sender not in settings.approver_numbers:
        return None
    waiting = session.execute(
        select(m.VendorRegistrationRequest.id, m.VendorRegistrationRequest.vendor_name)
        .where(m.VendorRegistrationRequest.status == m.REG_AWAITING_APPROVAL)
        .order_by(m.VendorRegistrationRequest.id)
    ).all()
    if not waiting:
        return None  # nothing waiting: perhaps a purchase order -- not ours
    verb = match.group("verb").lower()
    if len(waiting) > 1:
        word = "APPROVE" if verb.startswith("approve") else "REJECT"
        return ["Kaun sa vendor? Abhi ye approval ke liye ruke hain:\n"
                + "\n".join(f"• VR-{row_id} {name or ''}" for row_id, name in waiting)
                + f"\n\nReply *{word} VR-<number>*."]
    return _decide_vendor(sender, verb, waiting[0][0], " ".join(match.group("reason").split()), session)


def handle_reply(sender: str, text: str | None, session: Session) -> list[str] | None:
    """Replies for the sender when `text` is a decision, else None (not ours)."""
    button = _BUTTON.match(text or "")
    if button:
        return _button_tap(sender, button.group("verb").lower(), int(button.group("id")), session)
    reason = _reason_reply(sender, text, session)
    if reason is not None:
        return reason
    parsed = parse_reply(text)
    if parsed is None:
        bare = _bare_decision(sender, text, session)
        if bare is not None:
            return bare
        # Not a decision: if a vendor card never reached him, it goes now.
        if deliver_waiting_cards(sender, session) and _HELLO.match(text or ""):
            return []  # the card answered his "hi" -- no help menu on top
        return None
    verb, kind, row_id, reason = parsed
    if kind == "vr":
        if verb.startswith(("pass", "fail")):
            return [f"For vendor registrations reply APPROVE VR-{row_id} or REJECT VR-{row_id} <reason>."]
        return _decide_vendor(sender, verb, row_id, reason, session)
    if verb.startswith(("approve", "reject")):
        return [f"For ledgers reply PASS LG-{row_id} or FAIL LG-{row_id} <reason>."]
    return _decide_ledger(sender, verb, row_id, reason, session)
