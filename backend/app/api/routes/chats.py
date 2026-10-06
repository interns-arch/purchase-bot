"""Chats page: who is writing to the WhatsApp line, and what the bot answered.
Reads the permanent message log (`integrations/whatsapp/chat_log.py`)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.time_utils import ist_isoformat

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.integrations.whatsapp.models import (
    PurchaseTeamMember,
    WhatsAppChatMessage,
    WhatsAppRegisteredNumber,
)

router = APIRouter(prefix="/api/chats", tags=["chats"], dependencies=[Depends(get_current_user)])


def _directory(db: Session) -> dict[str, dict]:
    """number -> {"name": ..., "tags": [...]}, from every list the bot knows."""
    from backend.app.integrations.whatsapp.config import whatsapp_settings
    from backend.app.integrations.whatsapp.registry import normalize_number
    from backend.app.vendor_onboarding.config import vendor_onboarding_settings as onboarding
    from core.models import Customer, Vendor

    people: dict[str, dict] = {}

    def add(number: str, tag: str, name: str | None = None) -> None:
        key = normalize_number(number)
        if not key:
            return
        entry = people.setdefault(key, {"name": None, "tags": []})
        if name and not entry["name"]:
            entry["name"] = name
        if tag not in entry["tags"]:
            entry["tags"].append(tag)

    for number in whatsapp_settings.admin_phone_numbers:
        add(number, "admin")
    for number, label in onboarding.approvers.items():
        add(number, "approver", label)
    for number, label in onboarding.purchase_team.items():
        add(number, "purchase team", label)
    for number, label in onboarding.ledger_checkers.items():
        add(number, "accounts", label)
    for member in db.execute(select(PurchaseTeamMember)).scalars():
        add(member.whatsapp_number, "purchase team", member.name)
    rows = db.execute(
        select(WhatsAppRegisteredNumber.whatsapp_number, Vendor.name, Customer.name)
        .outerjoin(Vendor, Vendor.id == WhatsAppRegisteredNumber.vendor_id)
        .outerjoin(Customer, Customer.id == WhatsAppRegisteredNumber.customer_id)
    ).all()
    for number, vendor_name, customer_name in rows:
        if vendor_name:
            add(number, "vendor", vendor_name)
        elif customer_name:
            add(number, "customer", customer_name)
    return people


def _preview(row: WhatsAppChatMessage) -> str:
    text = (row.text or "").strip().replace("\n", " ")
    if row.kind in ("document", "image") and row.filename:
        text = f"📎 {row.filename}" + (f" — {text}" if text else "")
    return ("↩ " if row.direction == "out" else "") + text[:120]


@router.get("")
def list_chats(q: str = Query("", max_length=100), limit: int = Query(150, le=500), db: Session = Depends(get_db)) -> list[dict]:
    people = _directory(db)
    needle = q.strip().lower()

    stats = db.execute(
        select(
            WhatsAppChatMessage.whatsapp_number,
            func.count(WhatsAppChatMessage.id),
            func.max(WhatsAppChatMessage.created_at),
        ).group_by(WhatsAppChatMessage.whatsapp_number)
    ).all()
    # The newest message of each chat, by time.
    newest = {}
    for row in db.execute(
        select(WhatsAppChatMessage)
        .order_by(WhatsAppChatMessage.whatsapp_number, WhatsAppChatMessage.created_at.desc(), WhatsAppChatMessage.id.desc())
        .distinct(WhatsAppChatMessage.whatsapp_number)
    ).scalars() if db.bind.dialect.name == "postgresql" else []:
        newest[row.whatsapp_number] = row

    matching_numbers: set[str] | None = None
    if needle:
        matching_numbers = {
            number
            for (number,) in db.execute(
                select(WhatsAppChatMessage.whatsapp_number)
                .where(
                    or_(
                        func.lower(WhatsAppChatMessage.text).contains(needle),
                        func.lower(WhatsAppChatMessage.filename).contains(needle),
                        WhatsAppChatMessage.whatsapp_number.contains(needle),
                    )
                )
                .distinct()
            )
        }
        matching_numbers |= {n for n, p in people.items() if needle in (p.get("name") or "").lower()}


    chats = []
    for number, count, last_at in stats:
        if matching_numbers is not None and number not in matching_numbers:
            continue
        person = people.get(number, {})
        last = newest.get(number) or db.execute(
            select(WhatsAppChatMessage).where(WhatsAppChatMessage.whatsapp_number == number)
            .order_by(WhatsAppChatMessage.created_at.desc(), WhatsAppChatMessage.id.desc()).limit(1)
        ).scalars().first()
        chats.append(
            {
                "number": number,
                "name": person.get("name"),
                "tags": person.get("tags", []),
                "message_count": count,
                "last_at": ist_isoformat(last_at),
                "preview": _preview(last) if last else "",
                "last_status": last.status if last else None,
            }
        )
    chats.sort(key=lambda c: c["last_at"] or "", reverse=True)
    return chats[:limit]


@router.get("/{number}")
def get_chat(number: str, limit: int = Query(300, le=2000), before_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    query = select(WhatsAppChatMessage).where(WhatsAppChatMessage.whatsapp_number == number)
    if before_id:
        query = query.where(WhatsAppChatMessage.id < before_id)
    # By TIME (then id): history imported after the fact has later ids than
    # messages that actually came after it.
    rows = list(
        db.execute(
            query.order_by(WhatsAppChatMessage.created_at.desc(), WhatsAppChatMessage.id.desc()).limit(limit)
        ).scalars()
    )
    rows.reverse()
    person = _directory(db).get(number, {})
    return {
        "number": number,
        "name": person.get("name"),
        "tags": person.get("tags", []),
        "has_more": len(rows) == limit,
        "messages": [
            {
                "id": r.id,
                "direction": r.direction,
                "kind": r.kind,
                "text": r.text,
                "filename": r.filename,
                "has_file": r.direction == "in" and r.kind in ("document", "image") and bool(r.wamid),
                "status": r.status,
                "error": r.error,
                "created_at": ist_isoformat(r.created_at),
            }
            for r in rows
        ],
    }


@router.get("/messages/{message_id}/file")
def download_file(message_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """The file someone sent, from where the bot stored it."""
    from backend.app.documents.models import IncomingDocument

    row = db.get(WhatsAppChatMessage, message_id)
    if row is None or not row.wamid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No file for this message.")
    stored = db.execute(
        select(IncomingDocument.stored_path)
        .where(IncomingDocument.whatsapp_message_id == row.wamid, IncomingDocument.stored_path.is_not(None))
        .order_by(IncomingDocument.id.desc())
    ).scalars().first()
    candidates = []
    if stored:
        candidates.append(Path(stored))
        candidates += [Path(stored.replace("/incoming/", f"/{folder}/", 1)) for folder in ("processed", "failed", "archive")]
    for path in candidates:
        if path.exists():
            return FileResponse(path=path, filename=row.filename or path.name)
    raise HTTPException(status.HTTP_404_NOT_FOUND, "The file is not stored on the server.")
