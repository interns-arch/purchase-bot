"""The SALES team: who they are, and how the Founder registers them.

Founder, 3 Sep 2026: the sales team asks *"is this part in our inventory?"*
over WhatsApp and needs an instant answer. Their numbers live HERE, in their
own table -- deliberately NOT in the `WhatsAppRegisteredNumber` registry as
customers, because a customer number's message is imported as a customer
ORDER (see `customer_text_order`). A sales question must be ANSWERED, not
turned into an order.

    Founder texts:  register sales
    Bot replies:    "Send the list now..."
    Founder sends:  an Excel, or the names and numbers typed into the chat
    Bot replies:    exactly who was added / changed, then the full roster

Both paste layouts are accepted -- see `parse_member_text`.

Removal is its own explicit flow (`remove sales`), mirroring the
purchase-team rules: a list you send ADDS and CORRECTS, it never silently
drops anyone.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from backend.app.integrations.whatsapp import command_store, registry
from backend.app.integrations.whatsapp.contact_import import (
    _numbers_in_cell,
    parse_contact_text,
)
from backend.app.integrations.whatsapp.models import SalesTeamMember
from core.logging_setup import get_logger

logger = get_logger(__name__)

SALES_COMMANDS = {
    "register sales",
    "register sale",
    "sales team",
    "sale team",
    "update sales team",
    "register sales team",
}
REMOVE_SALES_COMMANDS = {"remove sales", "remove sale team", "remove sales team"}
SALES_COMMAND_KEY = "register_sales"
REMOVE_SALES_COMMAND_KEY = "remove_sales"


def is_sales_command_text(text: str | None) -> bool:
    return (text or "").strip().lower() in SALES_COMMANDS


def is_sales_caption(caption: str | None) -> bool:
    return (caption or "").strip().lower() in SALES_COMMANDS


def is_remove_sales_command_text(text: str | None) -> bool:
    return (text or "").strip().lower() in REMOVE_SALES_COMMANDS


def has_pending_sales_command(sender: str, window_minutes: float, session) -> bool:
    return (
        command_store.get_fresh_command(sender, window_minutes, session)
        == SALES_COMMAND_KEY
    )


def has_pending_remove_sales_command(sender: str, window_minutes: float, session) -> bool:
    return (
        command_store.get_fresh_command(sender, window_minutes, session)
        == REMOVE_SALES_COMMAND_KEY
    )


_HEADER_ONLY_LINE = re.compile(
    r"^(employee\s*name|emp\s*name|name|official\s*phone\s*(no\.?|number)?|"
    r"phone\s*(no\.?|number)?|mobile|contact|number|sr\.?\s*no\.?|s\.?\s*no\.?)$",
    re.IGNORECASE,
)


def parse_two_block_contacts(text: str | None) -> list[tuple[str, list[str]]]:
    """Pair a NAMES block with a NUMBERS block pasted one after the other.

    Copying a two-column sheet into WhatsApp produces exactly this -- every
    name first, then every number::

        Employee Name
        Amit Kumar
        Arun Kumar Sharma
        ...
        OFFICIAL PHONE NO
        9217030418
        9773900582
        ...

    Rows are paired BY POSITION, so this is accepted ONLY when the two blocks
    are the same length. A mismatch returns [] and the caller falls back to
    the line-by-line parser rather than guessing a pairing -- pairing the
    wrong number to a name would send someone else's stock answers to the
    wrong person. Column headings are ignored."""
    names: list[str] = []
    numbers: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _HEADER_ONLY_LINE.match(line):
            continue
        found = _numbers_in_cell(line)
        # A NUMBER line is essentially nothing but a number -- "Amit 92170"
        # is a name+number line and belongs to the other parser.
        leftovers = re.sub(r"[\d\s+\-()]+", "", line)
        if found and not leftovers:
            numbers.extend(found)
        else:
            names.append(line)

    if not names or len(names) != len(numbers):
        return []
    return [(name, [number]) for name, number in zip(names, numbers)]


def parse_member_text(text: str | None) -> list[tuple[str, list[str]]]:
    """Name+number rows from a typed message, accepting BOTH layouts: one
    member per line, or the two-block paste above."""
    return parse_contact_text(text) or parse_two_block_contacts(text)


def roster_lines(session) -> list[str]:
    members = list(
        session.execute(select(SalesTeamMember).order_by(SalesTeamMember.name)).scalars()
    )
    if not members:
        return ["Current sales team: (empty)"]
    lines = [f"Current sales team ({len(members)} member(s)):"]
    lines.extend(f"• {m.name} ({m.whatsapp_number})" for m in members)
    return lines


def apply_update(rows: list[tuple[str, list[str]]], session) -> tuple[str, dict]:
    """ADD/UPDATE sales-team members. Listed members are added or corrected;
    members NOT in the list are kept. Audited."""
    from backend.app.services import audit_service

    existing = list(session.execute(select(SalesTeamMember)).scalars())
    by_number = {m.whatsapp_number: m for m in existing}
    by_name = {m.name.strip().lower(): m for m in existing}

    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for name, numbers in rows:
        number = numbers[0] if numbers else ""
        if not number:
            skipped.append(f"{name} — no usable number")
            continue
        if number in seen:
            skipped.append(f"{name} — duplicate number in the list")
            continue
        seen.add(number)

        member = by_number.get(number)
        if member is not None:
            if member.name != name:
                updated.append(f"{member.name} → {name} ({number})")
                member.name = name
                by_name[name.strip().lower()] = member
            continue
        member = by_name.get(name.strip().lower())
        if member is not None:
            updated.append(f"{member.name}: {member.whatsapp_number} → {number}")
            del by_number[member.whatsapp_number]
            member.whatsapp_number = number
            by_number[number] = member
            continue
        member = SalesTeamMember(name=name, whatsapp_number=number)
        session.add(member)
        by_number[number] = member
        by_name[name.strip().lower()] = member
        added.append(f"{name} ({number})")
    session.flush()

    lines = [
        f"✅ Sales team updated — {len(added)} added, {len(updated)} changed. "
        "Members not in your list were kept."
    ]
    if added:
        lines.append("")
        lines.append("🆕 Added:")
        lines.extend(f"• {entry}" for entry in added)
    if updated:
        lines.append("")
        lines.append("✏️ Changed:")
        lines.extend(f"• {entry}" for entry in updated)
    if skipped:
        lines.append("")
        lines.append("ℹ️ Skipped:")
        lines.extend(f"• {entry}" for entry in skipped)
    lines.append("")
    lines.extend(roster_lines(session))
    lines.append("")
    lines.append(
        "These numbers can now ask stock questions directly — just send the "
        "part number(s). They get availability and quantity only, never the "
        "vendor name."
    )
    lines.append('To remove someone, text "remove sales" and send their row.')

    audit_service.record(
        session,
        actor="founder-whatsapp",
        action="sales_team_update",
        entity_type="sales_team",
        new_value={"added": added, "updated": updated, "skipped": skipped},
        reason=f"{len(rows)} row(s) in the sales team list",
    )
    return "\n".join(lines), {
        "added": len(added),
        "updated": len(updated),
        "skipped": len(skipped),
    }


def apply_removal(rows: list[tuple[str, list[str]]], session) -> tuple[str, dict]:
    """Remove the listed members (by number when given, else by name)."""
    from backend.app.services import audit_service

    existing = list(session.execute(select(SalesTeamMember)).scalars())
    by_number = {m.whatsapp_number: m for m in existing}
    by_name = {m.name.strip().lower(): m for m in existing}

    removed: list[str] = []
    not_found: list[str] = []
    for name, numbers in rows:
        member = None
        for number in numbers:
            member = by_number.get(number)
            if member is not None:
                break
        if member is None:
            member = by_name.get(name.strip().lower())
        if member is None:
            not_found.append(name)
            continue
        by_number.pop(member.whatsapp_number, None)
        by_name.pop(member.name.strip().lower(), None)
        removed.append(f"{member.name} ({member.whatsapp_number})")
        session.delete(member)
    session.flush()

    lines = [f"✅ Removed {len(removed)} member(s) from the sales team."]
    lines.extend(f"• {entry}" for entry in removed)
    if not_found:
        lines.append("")
        lines.append(f"⚠️ Not on the sales team: {', '.join(not_found)}")
    lines.append("")
    lines.extend(roster_lines(session))

    audit_service.record(
        session,
        actor="founder-whatsapp",
        action="sales_team_removal",
        entity_type="sales_team",
        previous_value={"removed": removed},
        new_value=None,
        reason=f"{len(rows)} row(s) in the removal list",
    )
    return "\n".join(lines), {"removed": len(removed), "not_found": len(not_found)}


def member_name(sender: str, session) -> str | None:
    """The member's NAME when this number is on the sales team, else None."""
    number = registry.normalize_number(sender)
    if not number:
        return None
    member = session.execute(
        select(SalesTeamMember).where(SalesTeamMember.whatsapp_number == number)
    ).scalar_one_or_none()
    return member.name if member is not None else None


def remember_customer(number: str, customer_name: str, session) -> None:
    """Record who this member last ordered for, so the next bare "confirm"
    can OFFER that name back."""
    normalized = registry.normalize_number(number)
    member = session.execute(
        select(SalesTeamMember).where(SalesTeamMember.whatsapp_number == normalized)
    ).scalar_one_or_none()
    if member is None:
        return
    from core.time_utils import utcnow_naive

    member.last_customer_name = (customer_name or "").strip() or None
    member.last_customer_at = utcnow_naive()
    session.flush()


def recall_customer(number: str, session, *, within_hours: float = 12.0) -> str | None:
    """The customer this member last ordered for, if recent enough to be
    worth offering.

    NEVER applied automatically -- the caller must show the name and get a
    yes. A sales person serves MANY customers, so a silent reuse would file
    the order against the wrong one; that is a costlier mistake than asking.
    An old memory is not offered at all: yesterday's customer is not a
    sensible suggestion for today's order."""
    from datetime import timedelta

    from core.time_utils import utcnow_naive

    normalized = registry.normalize_number(number)
    member = session.execute(
        select(SalesTeamMember).where(SalesTeamMember.whatsapp_number == normalized)
    ).scalar_one_or_none()
    if member is None or not member.last_customer_name:
        return None
    if within_hours > 0 and member.last_customer_at is not None:
        if utcnow_naive() - member.last_customer_at > timedelta(hours=within_hours):
            return None
    return member.last_customer_name
