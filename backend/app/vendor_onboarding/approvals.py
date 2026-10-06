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


def vendor_summary(request: m.VendorRegistrationRequest) -> str:
    answers = request.answers or {}
    lines = [f"New vendor registration {_code(request)}", ""]
    for question in QUESTIONS:
        value = answers.get(question.key)
        if value not in (None, "", []):
            lines.append(f"{question.label}: {format_answer(question.key, value)}")
    gst = request.gst_data or {}
    if gst:
        lines.append(f"\nGST portal: {gst.get('legal_name') or '-'} — {gst.get('status') or '-'}")
        if gst.get("registration_date"):
            lines.append(f"Registered since {gst['registration_date']}")
    else:
        lines.append("\nGST portal: not verified online (checksum OK)")
    answers = request.answers or {}
    if answers.get("_on_behalf"):
        lines.append(f"Filled by {answers.get('_created_by') or request.whatsapp_number} (purchase team) on behalf of the vendor")
    else:
        lines.append(f"Filled by the vendor from WhatsApp {request.whatsapp_number}")
    return "\n".join(lines)


def _code(request: m.VendorRegistrationRequest) -> str:
    return f"VR-{request.id}"


def request_vendor_approval(request: m.VendorRegistrationRequest, session: Session) -> None:
    from core.services import vendor_service

    code = _code(request)
    text = vendor_summary(request)
    similar = vendor_service.get_vendor_by_name(request.vendor_name or "", session)
    if similar is not None:
        text += f"\n\n⚠️ A vendor named '{similar.name}' already exists in ProcureHub — check it is not a duplicate."
    text += f"\n\nReply APPROVE {code} or REJECT {code} <reason>."
    if not settings.approver_numbers:
        logger.warning("%s submitted but VENDOR_APPROVER_NUMBERS is empty -- nobody was asked.", code)
    notify(
        settings.approver_numbers,
        text,
        template=settings.approval_template,
        params=[code, f"{request.vendor_name} ({request.gstin})"],
    )
    _publish("info", f"Vendor registration {code}", f"{request.vendor_name} is waiting for approval.")


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


def handle_reply(sender: str, text: str | None, session: Session) -> list[str] | None:
    """Replies for the sender when `text` is a decision, else None (not ours)."""
    parsed = parse_reply(text)
    if parsed is None:
        return None
    verb, kind, row_id, reason = parsed
    if kind == "vr":
        if verb.startswith(("pass", "fail")):
            return [f"For vendor registrations reply APPROVE VR-{row_id} or REJECT VR-{row_id} <reason>."]
        return _decide_vendor(sender, verb, row_id, reason, session)
    if verb.startswith(("approve", "reject")):
        return [f"For ledgers reply PASS LG-{row_id} or FAIL LG-{row_id} <reason>."]
    return _decide_ledger(sender, verb, row_id, reason, session)
