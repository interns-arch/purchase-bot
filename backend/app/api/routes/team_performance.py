"""Performance of vendors, the purchase team and the bot (Founder, 15 Oct
2026: "who is the most consistent vendor, how many times, and who asks for
it -- so I can judge the purchase team, the bot and the vendors").

    GET /api/team-performance?days=30

Everything is derived from what the bot already records -- stock imports, the
WhatsApp chat log, advance-order questions and Dealer Portal pushes -- so it
needs no extra data entry and covers the past as well.

Who SENT a stock file is found by matching each import to the WhatsApp file
it came from (same file name, within 15 minutes): the vendor's own number,
or a purchase-team member who forwarded it. Files with no WhatsApp match
came in by the website / e-mail."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.api.routes.chats import _directory
from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.integrations.whatsapp.models import WhatsAppChatMessage, WhatsAppRegisteredNumber
from core.models import DealerPortalPush, ImportStatus, InventoryImport, Vendor
from core.time_utils import now_ist_naive

router = APIRouter(prefix="/api/team-performance", tags=["performance"], dependencies=[Depends(get_current_user)])

_OK = {ImportStatus.COMPLETED, ImportStatus.COMPLETED_WITH_ERRORS, ImportStatus.SUPERSEDED}
_MATCH_WINDOW = timedelta(minutes=15)


def _key(name: str | None) -> str:
    """'STOCK HYUNDAI,TOYOTA,VW,HONDA.xlsx' and 'STOCK_HYUNDAI_TOYOTA_VW_HONDA.xlsx' -> same key."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name or "")
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def _hhmm(value: str, default: time) -> time:
    try:
        h, m = (int(x) for x in value.split(":"))
        return time(h, m)
    except (ValueError, AttributeError):
        return default


def _working_days(start: date, end: date) -> int:
    days = 0
    d = start
    while d <= end:
        if d.weekday() != 6:  # Sunday off
            days += 1
        d += timedelta(days=1)
    return max(days, 1)


@router.get("")
def team_performance(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)) -> dict:
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    now = now_ist_naive()
    start_day = (now - timedelta(days=days - 1)).date()
    start = datetime.combine(start_day, time(0, 0))
    working = _working_days(start_day, now.date())
    reminder_at = _hhmm(whatsapp_settings.auto_reminder_time or "12:30", time(12, 30))
    request_at = _hhmm(whatsapp_settings.daily_request_time or "09:30", time(9, 30))

    people = _directory(db)
    team_numbers = {
        n for n, p in people.items() if {"purchase team", "admin", "approver", "accounts"} & set(p["tags"])
    }
    vendor_of_number: dict[str, set[int]] = defaultdict(set)
    for number, vendor_id in db.execute(
        select(WhatsAppRegisteredNumber.whatsapp_number, WhatsAppRegisteredNumber.vendor_id).where(
            WhatsAppRegisteredNumber.vendor_id.isnot(None)
        )
    ):
        vendor_of_number[number].add(vendor_id)
    numbers_of_vendor: dict[int, set[str]] = defaultdict(set)
    for number, vids in vendor_of_number.items():
        for vid in vids:
            numbers_of_vendor[vid].add(number)

    # ---- WhatsApp files received, to attribute each import to its sender
    files = db.execute(
        select(WhatsAppChatMessage.whatsapp_number, WhatsAppChatMessage.filename, WhatsAppChatMessage.created_at)
        .where(
            WhatsAppChatMessage.direction == "in",
            WhatsAppChatMessage.filename.isnot(None),
            WhatsAppChatMessage.created_at >= start - _MATCH_WINDOW,
        )
        .order_by(WhatsAppChatMessage.created_at)
    ).all()
    files_by_key: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
    for number, filename, at in files:
        files_by_key[_key(filename)].append((number, at))

    def sender_of(imp: InventoryImport) -> str | None:
        """The WhatsApp number this import's file came from, if any."""
        best = None
        for number, at in files_by_key.get(_key(imp.file_name), []):
            if at <= imp.created_at + timedelta(minutes=1) and imp.created_at - at <= _MATCH_WINDOW:
                best = number  # latest match before the import
        if best is None and (imp.file_name or "").startswith("whatsapp_text_stock_"):
            tail = re.findall(r"(\d{4})\.csv$", imp.file_name or "")
            if tail:
                for number in numbers_of_vendor.get(imp.vendor_id, set()):
                    if number.endswith(tail[0]):
                        best = number
        return best

    def who(number: str | None, vendor_id: int) -> tuple[str, str]:
        """(kind, label): kind = vendor / team / other / web."""
        if number is None:
            return "web", "website / e-mail"
        if vendor_id in vendor_of_number.get(number, set()):
            return "vendor", "vendor itself"
        person = people.get(number) or {}
        if number in team_numbers:
            return "team", (person.get("name") or number)
        return "other", (person.get("name") or f"+{number}")

    imports = db.execute(
        select(InventoryImport).where(InventoryImport.created_at >= start).order_by(InventoryImport.created_at)
    ).scalars().all()
    vendor_names = dict(db.execute(select(Vendor.id, Vendor.name)).all())
    own_stock = set(db.execute(select(Vendor.id).where(Vendor.is_own_stock.is_(True))).scalars())

    v_stats: dict[int, dict] = {}
    team: dict[str, dict] = {}
    for imp in imports:
        if imp.vendor_id in own_stock:
            continue
        received = imp.status in _OK or (imp.status == ImportStatus.CANCELLED and imp.duplicate_of_import_id)
        failed = imp.status == ImportStatus.FAILED
        if not (received or failed):
            continue
        s = v_stats.setdefault(imp.vendor_id, {
            "vendor_id": imp.vendor_id, "vendor": vendor_names.get(imp.vendor_id, str(imp.vendor_id)),
            "days": set(), "on_time_days": set(), "first_times": {}, "files": 0, "failed": 0,
            "senders": Counter(), "last": None,
        })
        number = sender_of(imp)
        kind, label = who(number, imp.vendor_id)
        if failed:
            s["failed"] += 1
        else:
            day = imp.created_at.date()
            s["files"] += 1
            s["days"].add(day)
            first = s["first_times"].get(day)
            if first is None or imp.created_at.time() < first:
                s["first_times"][day] = imp.created_at.time()
            s["senders"][label] += 1
            s["last"] = max(s["last"] or imp.created_at, imp.created_at)
        if kind == "team":
            t = team.setdefault(number, {"number": number, "name": label, "files": 0, "vendors": set(), "days": set(), "failed": 0})
            t["files"] += 0 if failed else 1
            t["failed"] += 1 if failed else 0
            t["vendors"].add(vendor_names.get(imp.vendor_id, str(imp.vendor_id)))
            t["days"].add(imp.created_at.date())

    # ---- what the bot sent to each vendor (stock requests / reminders)
    asked: dict[int, Counter] = defaultdict(Counter)
    vendor_numbers = set(vendor_of_number)
    for number, at in db.execute(
        select(WhatsAppChatMessage.whatsapp_number, WhatsAppChatMessage.created_at).where(
            WhatsAppChatMessage.direction == "out",
            WhatsAppChatMessage.kind == "template",
            WhatsAppChatMessage.created_at >= start,
            WhatsAppChatMessage.whatsapp_number.in_(vendor_numbers or {""}),
        )
    ):
        kind = "reminders" if at.time() >= reminder_at else "requests"
        for vid in vendor_of_number.get(number, set()):
            asked[vid][kind] += 1

    # ---- Dealer Portal, latest per vendor
    portal: dict[int, dict] = {}
    for p in db.execute(
        select(DealerPortalPush).where(DealerPortalPush.created_at >= start).order_by(DealerPortalPush.id)
    ).scalars():
        for vid in p.vendor_ids or []:
            row = portal.setdefault(vid, {"uploads": 0, "accepted": 0, "last_status": None})
            row["uploads"] += 1
            row["accepted"] = (p.inserted_count or 0) + (p.updated_count or 0)
            row["last_status"] = p.status.value if hasattr(p.status, "value") else str(p.status)

    # Registered stock-sharing vendors who sent NOTHING still belong in the table.
    for vid in numbers_of_vendor:
        if vid not in v_stats and vid not in own_stock and vid in vendor_names:
            v_stats[vid] = {"vendor_id": vid, "vendor": vendor_names[vid], "days": set(), "on_time_days": set(),
                            "first_times": {}, "files": 0, "failed": 0, "senders": Counter(), "last": None}

    vendors_out = []
    for vid, s in v_stats.items():
        on_time = sum(1 for t in s["first_times"].values() if t < reminder_at)
        times = sorted(s["first_times"].values())
        median = times[len(times) // 2].strftime("%H:%M") if times else None
        self_count = s["senders"].get("vendor itself", 0)
        vendors_out.append({
            "vendor_id": vid,
            "vendor": s["vendor"],
            "days_sent": len(s["days"]),
            "working_days": working,
            "consistency": round(100 * len(s["days"]) / working),
            "on_time_days": on_time,
            "usual_time": median,
            "files": s["files"],
            "failed_files": s["failed"],
            "sent_by_vendor": self_count,
            "senders": [{"who": k, "files": n} for k, n in s["senders"].most_common()],
            "bot_requests": asked[vid]["requests"],
            "bot_reminders": asked[vid]["reminders"],
            "last_received": s["last"],
            "portal": portal.get(vid),
            "registered": vid in numbers_of_vendor,
        })
    vendors_out.sort(key=lambda r: (-r["consistency"], -r["on_time_days"], r["vendor"]))

    # ---- team: also count every message they sent the bot
    msgs = Counter(
        dict(
            db.execute(
                select(WhatsAppChatMessage.whatsapp_number, func.count())
                .where(
                    WhatsAppChatMessage.direction == "in",
                    WhatsAppChatMessage.created_at >= start,
                    WhatsAppChatMessage.whatsapp_number.in_(team_numbers or {""}),
                )
                .group_by(WhatsAppChatMessage.whatsapp_number)
            ).all()
        )
    )
    for number in team_numbers:
        if msgs.get(number) and number not in team:
            team[number] = {"number": number, "name": (people.get(number) or {}).get("name") or number,
                            "files": 0, "vendors": set(), "days": set(), "failed": 0}
    team_out = sorted(
        (
            {
                "name": t["name"],
                "number": t["number"],
                "roles": (people.get(t["number"]) or {}).get("tags", []),
                "files_forwarded": t["files"],
                "failed_files": t["failed"],
                "vendors_covered": len(t["vendors"]),
                "vendors": sorted(t["vendors"]),
                "active_days": len(t["days"]),
                "messages_to_bot": msgs.get(t["number"], 0),
            }
            for t in team.values()
        ),
        key=lambda r: (-r["files_forwarded"], -r["messages_to_bot"]),
    )

    # ---- the bot itself
    out_rows = db.execute(
        select(WhatsAppChatMessage.kind, WhatsAppChatMessage.status, func.count())
        .where(WhatsAppChatMessage.direction == "out", WhatsAppChatMessage.created_at >= start)
        .group_by(WhatsAppChatMessage.kind, WhatsAppChatMessage.status)
    ).all()
    sent_total = sum(n for _k, _s, n in out_rows)
    failed_msgs = sum(n for _k, s, n in out_rows if s == "failed")
    bot = {
        "stock_requests": sum(c["requests"] for c in asked.values()),
        "stock_reminders": sum(c["reminders"] for c in asked.values()),
        "files_imported": sum(v["files"] for v in vendors_out),
        "files_failed": sum(v["failed_files"] for v in vendors_out),
        "messages_sent": sent_total,
        "messages_failed": failed_msgs,
        "messages_received": db.execute(
            select(func.count()).select_from(WhatsAppChatMessage).where(
                WhatsAppChatMessage.direction == "in", WhatsAppChatMessage.created_at >= start
            )
        ).scalar() or 0,
    }
    try:
        from backend.app.advance_orders import models as m

        q = Counter(dict(db.execute(
            select(m.AdvanceVendorQuery.status, func.count())
            .where(m.AdvanceVendorQuery.created_at >= start)
            .group_by(m.AdvanceVendorQuery.status)
        ).all()))
        bot["enquiries_sent"] = sum(n for s, n in q.items() if s != m.Q_QUEUED)
        bot["enquiries_answered"] = q.get(m.Q_REPLIED, 0)
        bot["enquiries_unanswered"] = q.get(m.Q_TIMEOUT, 0)
        bot["advance_orders"] = db.execute(
            select(func.count()).select_from(m.AdvanceOrder).where(m.AdvanceOrder.created_at >= start)
        ).scalar() or 0
    except Exception:  # noqa: BLE001 -- advance orders optional
        pass
    pushes = Counter(dict(db.execute(
        select(DealerPortalPush.status, func.count())
        .where(DealerPortalPush.created_at >= start)
        .group_by(DealerPortalPush.status)
    ).all()))
    bot["portal_uploads"] = {getattr(k, "value", str(k)): n for k, n in pushes.items()}

    return {
        "days": days,
        "from": start_day,
        "to": now.date(),
        "working_days": working,
        "request_time": request_at.strftime("%H:%M"),
        "reminder_time": reminder_at.strftime("%H:%M"),
        "vendors": vendors_out,
        "team": team_out,
        "bot": bot,
    }
