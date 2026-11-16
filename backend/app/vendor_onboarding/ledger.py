"""Vendor ledgers: read the statement, check its arithmetic, send it to the
accounts team, and act on their PASS / FAIL.

Reading: Excel/CSV and text PDFs go to the AI as text; photos and scanned
PDFs go to the vision model. The AI only TRANSCRIBES -- every number it
returns is re-checked here (lines must add up to the totals, and opening +
debits - credits must equal the closing balance), so a misread figure is
caught before any human sees it."""

from __future__ import annotations

import mimetypes
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.outbound import send_reply_safe
from backend.app.vendor_onboarding import models as m
from backend.app.vendor_onboarding.config import vendor_onboarding_settings as settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

LEDGER_WORDS = {"ledger", "ledgers", "ledger account", "statement", "account statement", "ledger statement"}
TOLERANCE = Decimal("1.00")
MIN_CONFIDENCE = 0.70
_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

_SYSTEM = (
    "You transcribe Indian accounting ledger statements (Tally-style 'Ledger Account') into JSON. "
    "Copy numbers EXACTLY as printed; never calculate, round or invent. Indian digit grouping "
    "(2,05,808.00) means 205808.00. Use null for anything not printed. Reply with JSON only."
)
_USER = """Transcribe this ledger into exactly this JSON:
{
  "vendor_name": "the company that issued the ledger (top header)",
  "vendor_gstin": "its GST number or null",
  "party_name": "the account this ledger is for (e.g. CARTREND AUTO PARTS PRIVATE LIMITED)",
  "period_from": "YYYY-MM-DD", "period_to": "YYYY-MM-DD",
  "opening_balance": {"amount": 0.0, "side": "Dr or Cr"},
  "lines": [{"date": "YYYY-MM-DD or null", "particulars": "...", "vch_type": "...", "vch_no": "...",
             "debit": 0.0 or null, "credit": 0.0 or null}],
  "printed_total_debit": "the Debit column total printed above the closing balance, or null",
  "printed_total_credit": "the Credit column total printed above the closing balance, or null",
  "closing_balance": {"amount": 0.0, "side": "Dr or Cr"},
  "confidence": 0.0 to 1.0 (how sure you are every number is read correctly)
}
Rules: "lines" EXCLUDES the Opening Balance and Closing Balance rows and any total rows.
A row with no date of its own takes the date of the row above.
Opening balance printed in the Debit column is "Dr", in the Credit column "Cr".
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def is_ledger_word(text: str | None) -> bool:
    return " ".join((text or "").lower().split()) in LEDGER_WORDS


def code(submission: m.VendorLedgerSubmission) -> str:
    return f"LG-{submission.id}"


def _money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = value.get("amount")
    text = re.sub(r"[₹,\s]|dr|cr", "", str(value).lower())
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _date(value) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _side(value) -> str | None:
    side = str((value or {}).get("side") if isinstance(value, dict) else value or "").strip().lower()
    if side.startswith("d"):
        return "Dr"
    if side.startswith("c"):
        return "Cr"
    return None


def _inr(value: Decimal | None) -> str:
    """2,05,808.00 (Indian grouping)."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    whole, _, fraction = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{fraction}"


def _vendor_numbers(vendor_id: int | None, session: Session) -> list[str]:
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber

    if vendor_id is None:
        return []
    return list(
        session.execute(
            select(WhatsAppRegisteredNumber.whatsapp_number).where(WhatsAppRegisteredNumber.vendor_id == vendor_id)
        ).scalars()
    )


def _vendor_gstin(vendor_id: int | None, session: Session) -> str | None:
    """The vendor's GSTIN: from its approved registration, else from the
    contact info ProcureHub stored at creation."""
    if vendor_id is None:
        return None
    request = session.execute(
        select(m.VendorRegistrationRequest)
        .where(m.VendorRegistrationRequest.vendor_id == vendor_id, m.VendorRegistrationRequest.gstin.is_not(None))
        .order_by(m.VendorRegistrationRequest.id.desc())
    ).scalars().first()
    if request is not None:
        return request.gstin
    from core.models import Vendor

    vendor = session.get(Vendor, vendor_id)
    found = re.search(r"GSTIN:\s*([0-9A-Z]{15})", (vendor.contact_info or "") if vendor else "")
    return found.group(1) if found else None


def _is_staff(number: str) -> bool:
    from backend.app.integrations.whatsapp import daily_stock

    if number in settings.approver_numbers or number in settings.ledger_checker_numbers:
        return True
    if number in settings.ledger_escalation_numbers or settings.is_purchase_team(number):
        return True
    try:
        if daily_stock.is_admin_sender(number):
            return True
        from backend.app.integrations.whatsapp.recipients import internal_file_recipients

        return number in internal_file_recipients()
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def _ask(user: str, image: tuple[bytes, str] | None = None) -> tuple[dict | None, str | None]:
    from backend.app.ai import llm

    return llm.ask_json(_SYSTEM, user, purpose="vision", image=image)


def _merge_pages(pages: list[dict]) -> dict:
    """Page 1 carries the header and opening balance, the last page the
    closing balance; lines are concatenated in page order."""
    merged = dict(pages[0])
    merged["lines"] = [line for page in pages for line in (page.get("lines") or [])]
    last = pages[-1]
    for key in ("closing_balance", "printed_total_debit", "printed_total_credit", "period_to"):
        if last.get(key) not in (None, "", {}):
            merged[key] = last[key]
    merged["confidence"] = min(float(p.get("confidence") or 0) for p in pages)
    return merged


def read_ledger(file_path: Path) -> tuple[dict | None, str | None]:
    """(transcription, how it was read) -- (None, reason) when unreadable."""
    from backend.app.ai import compact

    suffix = file_path.suffix.lower()
    try:
        if suffix in {".xlsx", ".xlsm", ".xls", ".csv"}:
            if suffix == ".csv":
                from core.ingestion.csv_reader import read_csv_grid

                grid = read_csv_grid(file_path)
            else:
                from core.ingestion.excel_reader import read_excel_grid

                grid = read_excel_grid(file_path)
            text = compact.compact_grid(grid, file_name=file_path.name, max_rows=400, max_columns=12)
            data, provider = _ask(_USER + "\n\nLEDGER (spreadsheet cells):\n" + text)
            return data, provider and f"sheet/{provider}"

        if suffix == ".pdf":
            from core.ingestion.pdf_reader import read_pdf

            raw = ""
            try:
                raw = read_pdf(file_path).text or ""
            except Exception:  # noqa: BLE001 -- fall back to vision below
                raw = ""
            if len(raw.strip()) > 80:
                text = compact.compact_pdf_text(raw, file_name=file_path.name, max_chars=20_000)
                data, provider = _ask(_USER + "\n\nLEDGER (PDF text):\n" + text)
                return data, provider and f"pdf-text/{provider}"
            from backend.app.ai.vision import pdf_pages_as_images

            pages = []
            provider = None
            for png in pdf_pages_as_images(file_path.read_bytes(), max_pages=6):
                page, provider = _ask(_USER, image=(png, "image/png"))
                if page is None:
                    return None, "could not read a page of the PDF"
                pages.append(page)
            if not pages:
                return None, "the PDF has no pages"
            return _merge_pages(pages), provider and f"pdf-scan/{provider}"

        if suffix in _IMAGE_TYPES or (mimetypes.guess_type(file_path.name)[0] or "").startswith("image/"):
            mime = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
            data, provider = _ask(_USER, image=(file_path.read_bytes(), mime))
            return data, provider and f"photo/{provider}"
    except Exception:  # noqa: BLE001 -- reported to the sender as unreadable
        logger.exception("Reading ledger %s failed.", file_path.name)
        return None, "the file could not be read"
    return None, f"'{suffix or 'this'}' files are not supported — send a PDF, photo or Excel"


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def _fill(submission: m.VendorLedgerSubmission, data: dict) -> None:
    lines = []
    for raw in data.get("lines") or []:
        if not isinstance(raw, dict):
            continue
        lines.append(
            {
                "date": str(_date(raw.get("date")) or raw.get("date") or ""),
                "particulars": (raw.get("particulars") or "").strip(),
                "vch_type": (raw.get("vch_type") or "").strip(),
                "vch_no": str(raw.get("vch_no") or "").strip(),
                "debit": str(_money(raw.get("debit"))) if _money(raw.get("debit")) is not None else None,
                "credit": str(_money(raw.get("credit"))) if _money(raw.get("credit")) is not None else None,
            }
        )
    submission.lines = lines
    submission.ledger_vendor_name = (data.get("vendor_name") or "").strip() or None
    gstin = re.sub(r"\s", "", str(data.get("vendor_gstin") or "")).upper()
    submission.ledger_gstin = gstin or None
    submission.party_name = (data.get("party_name") or "").strip() or None
    submission.period_from = _date(data.get("period_from"))
    submission.period_to = _date(data.get("period_to"))
    submission.opening_balance = _money(data.get("opening_balance"))
    submission.opening_side = _side(data.get("opening_balance"))
    submission.closing_balance = _money(data.get("closing_balance"))
    # The closing row is printed on the OPPOSITE column to the balance it
    # carries ("By Closing Balance" in Credit = a Dr balance), so its side is
    # worked out from the arithmetic in `run_checks`, never read.
    submission.closing_side = None
    submission.debit_total = sum((Decimal(l["debit"]) for l in lines if l["debit"]), Decimal("0.00"))
    submission.credit_total = sum((Decimal(l["credit"]) for l in lines if l["credit"]), Decimal("0.00"))


def run_checks(submission: m.VendorLedgerSubmission, data: dict, session: Session) -> list[dict]:
    """[{name, ok (True/False/None=warning), detail}]."""
    checks: list[dict] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    confidence = float(data.get("confidence") or 0)
    add("Readable", confidence >= MIN_CONFIDENCE and bool(submission.lines),
        f"{len(submission.lines)} lines read (confidence {confidence:.0%})")

    opening, closing = submission.opening_balance, submission.closing_balance
    debits, credits = submission.debit_total or Decimal(0), submission.credit_total or Decimal(0)
    if opening is None or closing is None:
        add("Balance", False, "Opening or closing balance not found on the ledger")
    else:
        signed_open = opening if submission.opening_side != "Cr" else -opening
        expected = signed_open + debits - credits
        ok = abs(abs(expected) - closing) <= TOLERANCE
        submission.closing_side = "Dr" if expected >= 0 else "Cr"
        add("Balance", ok,
            f"Opening {_inr(opening)} {submission.opening_side or ''} + debits {_inr(debits)} − credits "
            f"{_inr(credits)} = {_inr(abs(expected))}; ledger says {_inr(closing)}")

    for side, total, printed in (
        ("Debit", debits, _money(data.get("printed_total_debit"))),
        ("Credit", credits, _money(data.get("printed_total_credit"))),
    ):
        if printed is None:
            continue
        # Tally's column total may include the opening balance on its side.
        allowed = {total}
        if opening is not None and submission.opening_side == ("Dr" if side == "Debit" else "Cr"):
            allowed.add(total + opening)
        ok = any(abs(printed - value) <= TOLERANCE for value in allowed)
        add(f"{side} total", ok, f"Lines add up to {_inr(total)}; printed total {_inr(printed)}")

    party = (submission.party_name or "").upper().replace(" ", "")
    add("Addressed to CarTrend", "CARTREND" in party if party else None,
        submission.party_name or "Party name not found")

    expected_gstin = _vendor_gstin(submission.vendor_id, session)
    if expected_gstin and submission.ledger_gstin:
        add("Vendor GSTIN", submission.ledger_gstin == expected_gstin,
            f"Ledger {submission.ledger_gstin}, registered {expected_gstin}")
    else:
        add("Vendor GSTIN", None, f"Ledger GSTIN {submission.ledger_gstin or 'not printed'} (no registered GSTIN to compare)")

    if submission.vendor_id and submission.period_to:
        earlier = session.execute(
            select(m.VendorLedgerSubmission).where(
                m.VendorLedgerSubmission.vendor_id == submission.vendor_id,
                m.VendorLedgerSubmission.id != submission.id,
                m.VendorLedgerSubmission.status == m.LG_PASSED,
                m.VendorLedgerSubmission.period_to == submission.period_to,
            )
        ).scalars().first()
        if earlier is not None:
            add("Not a repeat", None, f"A ledger to the same date was already accepted ({code(earlier)})")
    return checks


def summary(submission: m.VendorLedgerSubmission, vendor_name: str) -> str:
    period = (
        f"{submission.period_from:%d-%b-%y} to {submission.period_to:%d-%b-%y}"
        if submission.period_from and submission.period_to
        else "period not printed"
    )
    version = f" (corrected, v{submission.version})" if submission.version > 1 else ""
    lines = [
        f"Vendor ledger {code(submission)}{version}: {vendor_name}, {period}",
        "",
        f"Opening: {_inr(submission.opening_balance)} {submission.opening_side or ''}",
        f"Debits:  {_inr(submission.debit_total)}",
        f"Credits: {_inr(submission.credit_total)}",
        f"Closing: {_inr(submission.closing_balance)} {submission.closing_side or ''}",
        f"Entries: {len(submission.lines or [])}",
        "",
        "Automatic checks:",
    ]
    for check in submission.checks or []:
        mark = "✅" if check["ok"] is True else ("❌" if check["ok"] is False else "⚠️")
        lines.append(f"{mark} {check['name']}: {check['detail']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# flow
# --------------------------------------------------------------------------


def receive(sender: str, file_path: Path, filename: str | None, session: Session) -> list[str]:
    """A ledger file has arrived from `sender`. Returns replies for the sender."""
    from backend.app.integrations.whatsapp import registry

    party = registry.lookup(sender, session)
    vendor_id = party.party_id if party is not None and party.party_type == "vendor" else None
    submission = m.VendorLedgerSubmission(
        sender=sender,
        vendor_id=vendor_id,
        status=m.LG_AWAITING_VENDOR,
        file_path=str(file_path),
        original_filename=filename,
    )
    session.add(submission)
    session.flush()
    if vendor_id is None:
        who = "Which vendor is this ledger from?" if _is_staff(sender) else "Which company is this ledger from?"
        return [f"Got the ledger ({code(submission)}). {who} Reply with the vendor name."]
    return process(submission, session)


def awaiting_vendor(sender: str, session: Session) -> m.VendorLedgerSubmission | None:
    return session.execute(
        select(m.VendorLedgerSubmission)
        .where(m.VendorLedgerSubmission.sender == sender, m.VendorLedgerSubmission.status == m.LG_AWAITING_VENDOR)
        .order_by(m.VendorLedgerSubmission.id.desc())
    ).scalars().first()


def provide_vendor_name(sender: str, text: str, session: Session) -> list[str] | None:
    """The sender's answer to "which vendor?", or None when nothing waits."""
    from core.services import vendor_service

    submission = awaiting_vendor(sender, session)
    if submission is None:
        return None
    name = (text or "").strip()
    if name.lower() in {"cancel", "stop"}:
        submission.status = m.LG_CHECK_FAILED
        submission.reason = "Cancelled by sender"
        return [f"{code(submission)} cancelled."]
    vendor = vendor_service.get_vendor_by_name(name, session)
    if vendor is None:
        options = vendor_service.suggest_vendor_names(name, session)
        hint = ("Did you mean: " + ", ".join(options) + "?") if options else "Please check the spelling."
        return [f"No vendor named '{name}'. {hint} Reply with the exact vendor name (or CANCEL)."]
    submission.vendor_id = vendor.id
    session.flush()
    return process(submission, session)


def process(submission: m.VendorLedgerSubmission, session: Session) -> list[str]:
    from backend.app.vendor_onboarding import approvals
    from core.models import Vendor

    vendor = session.get(Vendor, submission.vendor_id)
    vendor_name = vendor.name if vendor else "?"

    previous = session.execute(
        select(m.VendorLedgerSubmission)
        .where(
            m.VendorLedgerSubmission.vendor_id == submission.vendor_id,
            m.VendorLedgerSubmission.id != submission.id,
            m.VendorLedgerSubmission.status == m.LG_FAILED,
        )
        .order_by(m.VendorLedgerSubmission.id.desc())
    ).scalars().first()
    if previous is not None:
        submission.previous_id = previous.id
        submission.version = (previous.version or 1) + 1

    data, read_by = read_ledger(Path(submission.file_path))
    if data is None:
        submission.status = m.LG_CHECK_FAILED
        submission.reason = read_by
        session.flush()
        return [
            f"Sorry, I couldn't read this ledger ({read_by or 'unreadable'}). "
            "Please send a clear photo of each page, or the PDF/Excel from your accounting software, "
            "with the caption LEDGER."
        ]
    submission.read_by = read_by
    _fill(submission, data)
    submission.checks = run_checks(submission, data, session)
    failed = [c for c in submission.checks if c["ok"] is False]
    if failed:
        submission.status = m.LG_CHECK_FAILED
        submission.reason = "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
        session.flush()
        logger.info("%s failed the automatic checks: %s", code(submission), submission.reason)
        problems = "\n".join(f"❌ {c['name']}: {c['detail']}" for c in failed)
        return [
            f"Ledger {code(submission)} was not sent to accounts because it doesn't add up:\n{problems}\n\n"
            "Please check it and send a corrected/clearer copy with the caption LEDGER."
        ]
    submission.status = m.LG_AWAITING_ACCOUNTS
    session.flush()
    approvals.request_ledger_check(submission, summary(submission, vendor_name), session)
    return [f"Thank you! Ledger {code(submission)} for {vendor_name} has been sent to our accounts team for checking."]


def _tell_vendor_side(submission: m.VendorLedgerSubmission, text: str, session: Session) -> None:
    numbers = set(_vendor_numbers(submission.vendor_id, session))
    numbers.add(submission.sender)
    for number in numbers:
        send_reply_safe(number, text)


def on_pass(submission: m.VendorLedgerSubmission, checker: str, session: Session) -> str:
    from backend.app.vendor_onboarding import approvals, dealer_portal_vendor
    from core.models import Vendor

    vendor = session.get(Vendor, submission.vendor_id)
    registration = session.execute(
        select(m.VendorRegistrationRequest)
        .where(m.VendorRegistrationRequest.vendor_id == submission.vendor_id)
        .order_by(m.VendorRegistrationRequest.id.desc())
    ).scalars().first()
    result = dealer_portal_vendor.upload_ledger(
        submission,
        vendor_code=vendor.vendor_code if vendor else None,
        dealer_portal_vendor_ref=registration.dealer_portal_ref if registration else None,
    )
    submission.dealer_portal_ref = result.reference
    submission.dealer_portal_error = result.error
    submission.status = m.LG_PASSED if result.ok else m.LG_PUSH_FAILED
    session.flush()

    period = f" up to {submission.period_to:%d-%b-%y}" if submission.period_to else ""
    _tell_vendor_side(submission, f"Your ledger{period} ({code(submission)}) has been checked and accepted. Thank you!", session)
    approvals.notify(
        settings.ledger_escalation_numbers,
        f"Ledger {code(submission)} ({vendor.name if vendor else '?'}{period}) PASSED by {checker}."
        + ("" if result.ok else f" Dealer Portal upload failed: {result.error}"),
    )
    if not result.ok:
        return f"Passed {code(submission)}, but the Dealer Portal upload failed: {result.error}. It can be retried from the Vendor Onboarding page."
    portal = "Dealer Portal upload pending (API not connected yet)." if result.reference == "DRY-RUN" else "Uploaded to the Dealer Portal."
    return f"Passed {code(submission)}. The vendor has been told. {portal}"


def on_fail(submission: m.VendorLedgerSubmission, checker: str, reason: str, session: Session) -> str:
    from backend.app.vendor_onboarding import approvals
    from core.models import Vendor

    vendor = session.get(Vendor, submission.vendor_id)
    submission.status = m.LG_FAILED
    submission.reason = reason
    session.flush()
    _tell_vendor_side(
        submission,
        f"Our accounts team found a problem in your ledger {code(submission)}:\n{reason}\n\n"
        "Please send the corrected ledger here with the caption LEDGER.",
        session,
    )
    approvals.notify(
        settings.ledger_escalation_numbers,
        f"Ledger {code(submission)} ({vendor.name if vendor else '?'}) FAILED by {checker}: {reason}\n"
        "The vendor has been asked for a corrected ledger.",
    )
    return f"Failed {code(submission)}. The vendor has been asked for a corrected ledger and Prateek sir informed."
