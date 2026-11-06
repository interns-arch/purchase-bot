"""Groups page: the WhatsApp groups the purchase bot sits in (Whapi), read
live from Whapi -- every message, the team's own chat included.

Only the bot's groups (team group, vendor groups, WHAPI_GROUPS) can be
opened: the linked phone is a person's own WhatsApp, and none of their other
groups or personal chats is ever shown here."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.integrations.whatsapp import whapi
from core.logging_setup import get_logger
from core.time_utils import ist_isoformat

logger = get_logger(__name__)

router = APIRouter(prefix="/api/groups", tags=["groups"], dependencies=[Depends(get_current_user)])

_NAME_SECONDS = 10 * 60
_lock = threading.Lock()
_names_cache: tuple[float, dict[str, str]] | None = None


def _require() -> None:
    if not whapi.enabled():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="WhatsApp groups (Whapi) are not switched on.")


def _role(group_id: str) -> str:
    if group_id == whapi.team_group():
        return "team"
    code = whapi.vendor_group_codes().get(group_id)
    return f"vendor {code}" if code else "group"


def _all_names() -> dict[str, str]:
    """{group id: name} for every group the linked number is in -- ONE Whapi
    call, cached 10 minutes. (One call per group took 40 s+ for 44 groups
    and the page never finished loading, 2 Nov 2026.)"""
    global _names_cache
    now = time.monotonic()
    with _lock:
        if _names_cache and now - _names_cache[0] < _NAME_SECONDS:
            return _names_cache[1]
    try:
        names = {g.get("id"): (g.get("name") or g.get("id")) for g in whapi.list_groups() if g.get("id")}
    except Exception:  # noqa: BLE001 -- stale names beat no page
        logger.exception("Groups page: could not list the groups.")
        names = _names_cache[1] if _names_cache else {}
    with _lock:
        _names_cache = (now, names)
    return names


def _name(group_id: str) -> str:
    return _all_names().get(group_id) or group_id


def _when(timestamp) -> str | None:
    try:
        return ist_isoformat(datetime.fromtimestamp(int(timestamp), tz=timezone.utc))
    except (TypeError, ValueError, OSError):
        return None


def _owner_name() -> str:
    """Whose WhatsApp the linked phone is (Rajkumar's since 2 Nov 2026)."""
    return (os.environ.get("WHAPI_OWNER_NAME") or "Owner").strip()


def _who(msg: dict) -> dict:
    """Who wrote it: the bot (sent through the API), Alam sir on his phone,
    or a member -- named the way the rest of ProcureHub names them."""
    if msg.get("source") == "api":
        return {"by": "bot", "name": f"Bot (as {_owner_name()})", "number": msg.get("from")}
    number = str(msg.get("from") or "").split("@", 1)[0]
    if msg.get("from_me"):
        return {"by": "owner", "name": _owner_name(), "number": number}
    name = msg.get("from_name") or None
    try:
        from backend.app.integrations.whatsapp.registry import normalize_number
        from backend.app.vendor_onboarding.config import vendor_onboarding_settings

        name = vendor_onboarding_settings.honorific(normalize_number(number) or number) or name
    except Exception:  # noqa: BLE001
        pass
    return {"by": "team" if number and whapi._is_internal(number) else "member", "name": name or number, "number": number}


def _message(msg: dict) -> dict:
    kind = msg.get("type") or "text"
    media = msg.get(kind) if isinstance(msg.get(kind), dict) else {}
    text = (msg.get("text") or {}).get("body") if kind == "text" else (media.get("caption") or None)
    return {
        "id": msg.get("id"),
        "kind": kind,
        "text": text or ("" if kind in ("document", "image") else f"[{kind}]"),
        "filename": media.get("file_name") or media.get("filename") if kind in ("document", "image") else None,
        "media_id": media.get("id") if kind in ("document", "image") else None,
        "created_at": _when(msg.get("timestamp")),
        **_who(msg),
    }


def _messages(group_id: str, count: int) -> list[dict]:
    raw = whapi._request("GET", f"/messages/list/{group_id}", params={"count": count}).json().get("messages") or []
    raw.sort(key=lambda m: int(m.get("timestamp") or 0))
    return [_message(m) for m in raw if m.get("type") not in ("action", "system", "call")]


@router.get("")
def list_groups(db: Session = Depends(get_db)) -> list[dict]:
    """The bot's groups: names from one Whapi listing, the last thing the bot
    did in each from its own feed (no per-group Whapi calls)."""
    from sqlalchemy import func, select

    from backend.app.integrations.whatsapp.models import WhapiActivity, WhapiVendorGroup

    _require()
    names = _all_names()
    db_names = {g: n for g, n in db.execute(select(WhapiVendorGroup.group_id, WhapiVendorGroup.name)) if n}
    newest = (
        select(WhapiActivity.group_id, func.max(WhapiActivity.id).label("mid"))
        .where(WhapiActivity.group_id != "")
        .group_by(WhapiActivity.group_id)
        .subquery()
    )
    latest = {
        row.group_id: row
        for row in db.execute(select(WhapiActivity).join(newest, WhapiActivity.id == newest.c.mid)).scalars()
    }
    owner = os.environ.get("WHAPI_OWNER_NUMBER", "").strip()
    out = []
    for group_id in whapi.allowed_groups():
        last = latest.get(group_id)
        who = _who({"from": last.who, "from_me": last.who == owner})["name"] if last and last.who else None
        out.append({
            "id": group_id,
            "name": names.get(group_id) or db_names.get(group_id) or group_id,
            "role": _role(group_id),
            "member": group_id in names if names else True,
            "last_at": ist_isoformat(last.created_at) if last else None,
            "preview": ((f"{who}: " if who else "") + (last.text or ""))[:120] if last else "",
        })
    # Team group first, then the most recently active.
    return [g for g in out if g["role"] == "team"] + sorted(
        [g for g in out if g["role"] != "team"], key=lambda g: (g["last_at"] or "", g["name"]), reverse=True
    )


def _group_label(group_id: str, names: dict[str, str]) -> str:
    if not group_id:
        return "All vendor groups"
    if group_id == whapi.team_group():
        return f"Team · {_name(group_id)}"
    return names.get(group_id) or _name(group_id)


@router.get("/activity")
def activity(after: int = Query(0, ge=0), limit: int = Query(80, ge=1, le=300), db: Session = Depends(get_db)) -> list[dict]:
    """The live feed: what the bot took from which group and what it posted,
    newest first. `after` = the newest id the page already has."""
    from sqlalchemy import select

    from backend.app.integrations.whatsapp.models import WhapiActivity, WhapiVendorGroup

    _require()
    names = {g: n for g, n in db.execute(select(WhapiVendorGroup.group_id, WhapiVendorGroup.name)) if n}
    rows = db.execute(
        select(WhapiActivity).where(WhapiActivity.id > after).order_by(WhapiActivity.id.desc()).limit(limit)
    ).scalars().all()
    out = []
    for row in rows:
        who = None
        if row.who:
            who = _who({"from": row.who, "from_me": row.who == os.environ.get("WHAPI_OWNER_NUMBER", "").strip()})["name"]
        out.append({
            "id": row.id,
            "created_at": ist_isoformat(row.created_at),
            "group_id": row.group_id,
            "group": _group_label(row.group_id, names),
            "kind": row.kind,
            "who": who,
            "text": row.text,
        })
    return out


@router.get("/{group_id}")
def get_group(group_id: str, count: int = Query(150, ge=1, le=500)) -> dict:
    _require()
    if group_id not in whapi.allowed_groups():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not one of the bot's groups.")
    try:
        messages = _messages(group_id, count)
    except whapi.WhapiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Whapi did not answer: {exc}") from exc
    return {"id": group_id, "name": _name(group_id), "role": _role(group_id), "messages": messages}


@router.get("/media/{media_id}")
def group_file(media_id: str) -> Response:
    _require()
    try:
        content = whapi.download(media_id)
    except whapi.WhapiError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That file is no longer on WhatsApp.") from exc
    return Response(content=content, media_type="application/octet-stream")
