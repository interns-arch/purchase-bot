"""Founder-managed vendor contact updates, over WhatsApp or from a file.

The Founder (any admin number) can maintain the number registry without
touching the server:

    Founder texts:  register            (or "update numbers" / "contacts")
    Bot replies:    "Send the contact list Excel now ..."
    Founder sends:  an Excel with Vendor Name + WhatsApp number(s) per row
                    (a caption of "register"/"contacts" on the file works
                    without the text step too)
    Bot replies:    a summary of exactly what changed

Semantics (the Founder's list is AUTHORITATIVE):
- Each listed vendor's registered numbers are REPLACED by the numbers on
  their row -- so a changed number stops being messaged the same day.
- A number currently registered to a different vendor is re-pointed to the
  listed vendor (and reported).
- Two rows sharing one number are the SAME party: the first row wins, the
  later row is reported and skipped -- no second vendor is ever created
  (e.g. Brite Autocars / Brite Autowheels).
- Unknown vendor names are onboarded through the one existing mechanism
  (permanent Vendor Code, race-safe).

File format is forgiving: the vendor name in the first column; numbers taken
from a column whose header says "updated" when one exists (so a sheet
carrying both old PHONE and corrected numbers uses the corrected ones),
otherwise from phone/contact/number-titled columns, otherwise from every
cell after the name. Cells may hold several numbers separated by , / ; or
newlines. `backend/scripts/register_vendor_numbers.py` uses this same
parser, so the CLI seed and the WhatsApp flow can never disagree."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp import command_store, registry
from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
from core.logging_setup import get_logger

logger = get_logger(__name__)

# Exact phrases (case-insensitive) that start the contact-update flow --
# deliberately narrow so a vendor name could never trigger it by accident.
UPDATE_COMMANDS = {
    "register",
    "contacts",
    "update numbers",
    "update number",
    "update contacts",
    "update vendor numbers",
}

_CHUNK_SPLIT = re.compile(r"[,/;\n]+")
_HEADER_NAME_TOKENS = {"vendor", "name", "party", "customer"}
_HEADER_PHONE_TOKENS = {"phone", "mobile", "whatsapp", "contact", "number", "no"}


def _tokens(value) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", str(value or "").strip().lower())) - {""}


def _numbers_in_cell(value) -> list[str]:
    """Every usable WhatsApp number in one cell ('98 111, 98222/98333')."""
    if value is None:
        return []
    numbers = []
    for chunk in _CHUNK_SPLIT.split(str(value)):
        digits = re.sub(r"\D+", "", chunk)
        if 10 <= len(digits) <= 15:
            normalized = registry.normalize_number(digits)
            if normalized:
                numbers.append(normalized)
    return numbers


def parse_contact_rows(file_path: Path) -> list[tuple[str, list[str]]]:
    """[(vendor_name, [normalized numbers])] from a contact-list workbook.
    Blank/garbage rows are skipped; iteration stops after a long blank run
    (Excel sheets often report millions of phantom empty rows)."""
    import openpyxl

    workbook = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    worksheet = workbook.worksheets[0]

    rows: list[tuple[str, list[str]]] = []
    number_columns: list[int] | None = None  # None until/unless a header says
    header_seen = False
    blank_run = 0

    for row in worksheet.iter_rows(values_only=True):
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            blank_run += 1
            if blank_run >= 50:
                break
            continue
        blank_run = 0

        first = str(row[0] or "").strip()
        if not header_seen and _tokens(first) & _HEADER_NAME_TOKENS and not _numbers_in_cell(first):
            # Header row: pick the number column(s). "updated ..." wins so a
            # sheet with both the old PHONE and a corrected column uses the
            # corrected numbers only.
            header_seen = True
            updated_cols = [
                i for i, cell in enumerate(row) if "updated" in _tokens(cell)
            ]
            phone_cols = [
                i
                for i, cell in enumerate(row)
                if i > 0
                and (_tokens(cell) & _HEADER_PHONE_TOKENS)
                and "brands" not in _tokens(cell)
            ]
            number_columns = updated_cols or phone_cols or None
            continue

        if not first:
            continue
        cells = (
            [row[i] if i < len(row) else None for i in number_columns]
            if number_columns
            else list(row[1:])
        )
        numbers: list[str] = []
        for cell in cells:
            for number in _numbers_in_cell(cell):
                if number not in numbers:
                    numbers.append(number)
        rows.append((first, numbers))
    return rows


def apply_contact_update(
    rows: list[tuple[str, list[str]]], session: Session
) -> tuple[str, dict]:
    """Apply the Founder's authoritative contact list. Returns
    (whatsapp_reply_text, stats). Never partially silent: every skip,
    re-point and onboarding is in the reply."""
    # Imported here (not at module top) to keep this module import-light for
    # the CLI seed script; the dispatcher pulls in the whole import engine.
    from backend.app.services.document_processor.dispatcher import _resolve_or_onboard_vendor

    updated: list[str] = []
    onboarded: list[str] = []
    skipped: list[str] = []
    repointed: list[str] = []
    no_number: list[str] = []
    assigned_this_batch: dict[str, str] = {}  # number -> vendor name (first row wins)

    for name, numbers in rows:
        if not numbers:
            no_number.append(name)
            continue
        already = [n for n in numbers if n in assigned_this_batch]
        if len(already) == len(numbers):
            # Every number on this row already belongs to an earlier row --
            # same party under a second name (e.g. the two Brite companies).
            skipped.append(f"{name} — same number as {assigned_this_batch[already[0]]} (one vendor)")
            continue

        vendor, onboarding_message = _resolve_or_onboard_vendor(name, session)
        if onboarding_message:
            onboarded.append(f"{vendor.name} ({vendor.vendor_code})")

        # REPLACE: this vendor's registrations become exactly this row.
        for existing in session.execute(
            select(WhatsAppRegisteredNumber).where(
                WhatsAppRegisteredNumber.vendor_id == vendor.id
            )
        ).scalars():
            if existing.whatsapp_number not in numbers:
                session.delete(existing)
        session.flush()

        final_numbers = []
        for number in numbers:
            if assigned_this_batch.get(number) not in (None, vendor.name):
                continue  # first row keeps a shared number
            try:
                registry.register_vendor_number(number, vendor.id, session, note="founder update")
            except registry.NumberAlreadyRegisteredError as exc:
                # Registered to ANOTHER vendor from a previous run -- the
                # Founder's new list is authoritative: re-point it.
                registry.unregister(number, session)
                registry.register_vendor_number(number, vendor.id, session, note="founder update")
                repointed.append(
                    f"{number} → {vendor.name} (was vendor_id={exc.existing.vendor_id})"
                )
            except ValueError:
                continue  # admin number etc. -- refused, not fatal
            assigned_this_batch[number] = vendor.name
            final_numbers.append(number)
        if final_numbers:
            updated.append(f"{vendor.name} ({vendor.vendor_code}): {', '.join(final_numbers)}")

    lines = [f"✅ Vendor contacts updated — {len(updated)} vendor(s)."]
    lines.extend(f"• {entry}" for entry in updated)
    if onboarded:
        lines.append("")
        lines.append(f"🆕 New vendor(s) onboarded: {', '.join(onboarded)}")
    if repointed:
        lines.append("")
        lines.append("↪️ Number(s) moved:")
        lines.extend(f"• {entry}" for entry in repointed)
    if skipped:
        lines.append("")
        lines.append("ℹ️ Skipped:")
        lines.extend(f"• {entry}" for entry in skipped)
    if no_number:
        lines.append("")
        lines.append(f"⚠️ No usable number found for: {', '.join(no_number)}")
    stats = {
        "updated": len(updated),
        "onboarded": len(onboarded),
        "repointed": len(repointed),
        "skipped": len(skipped),
        "no_number": len(no_number),
    }
    # Management audit (spec §24): registry changes alter who can upload
    # stock directly, so every founder update is recorded with its outcome.
    from backend.app.services import audit_service

    audit_service.record(
        session,
        actor="founder-whatsapp",
        action="contact_registry_update",
        entity_type="whatsapp_registry",
        previous_value=None,
        new_value={"updated": updated, "repointed": repointed, "skipped": skipped},
        reason=f"{len(rows)} row(s) in the uploaded contact list",
    )
    return "\n".join(lines), stats


def is_update_command_text(text: str | None) -> bool:
    return (text or "").strip().lower() in UPDATE_COMMANDS


def is_update_caption(caption: str | None) -> bool:
    return (caption or "").strip().lower() in UPDATE_COMMANDS


REGISTER_COMMAND_KEY = "register"
TEAM_COMMAND_KEY = "register_team"

TEAM_COMMANDS = {"register team", "team", "update team", "purchase team"}


def is_team_command_text(text: str | None) -> bool:
    return (text or "").strip().lower() in TEAM_COMMANDS


def apply_team_update(rows: list[tuple[str, list[str]]], session) -> tuple[str, dict]:
    """REPLACE the purchase-team list with the uploaded Name + Number sheet
    (the Founder's clarification: internal purchasers who receive every PO
    as a CC). Same parser as the vendor contact flow; audited."""
    from backend.app.integrations.whatsapp.models import PurchaseTeamMember
    from backend.app.services import audit_service
    from sqlalchemy import select as _select

    for existing in session.execute(_select(PurchaseTeamMember)).scalars():
        session.delete(existing)
    session.flush()

    added: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for name, numbers in rows:
        number = numbers[0] if numbers else ""
        if not number:
            skipped.append(f"{name} — no usable number")
            continue
        if number in seen:
            skipped.append(f"{name} — duplicate number")
            continue
        seen.add(number)
        session.add(PurchaseTeamMember(name=name, whatsapp_number=number))
        added.append(f"{name} ({number})")
    session.flush()

    lines = [f"✅ Purchase team updated — {len(added)} member(s)."]
    lines.extend(f"• {entry}" for entry in added)
    if skipped:
        lines.append("")
        lines.append("ℹ️ Skipped:")
        lines.extend(f"• {entry}" for entry in skipped)
    lines.append("")
    lines.append("Every generated PO will now be sent to these members on WhatsApp.")

    audit_service.record(
        session,
        actor="founder-whatsapp",
        action="purchase_team_update",
        entity_type="purchase_team",
        new_value=added,
        reason=f"{len(rows)} row(s) in the uploaded team list",
    )
    return "\n".join(lines), {"added": len(added), "skipped": len(skipped)}


def has_pending_team_command(sender: str, window_minutes: float, session) -> bool:
    return (
        command_store.get_fresh_command(sender, window_minutes, session)
        == TEAM_COMMAND_KEY
    )


def has_pending_register_command(sender: str, window_minutes: float, session: Session) -> bool:
    return (
        command_store.get_fresh_command(sender, window_minutes, session)
        == REGISTER_COMMAND_KEY
    )
