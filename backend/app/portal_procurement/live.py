"""Live Active Procurement view for the dashboard (User, 16 Nov 2026):
every part the bot is working on, its brand, the vendors of that brand, who
was asked and who replied, the best answer and why it can or cannot win."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as am
from backend.app.portal_procurement import models as m
from backend.app.portal_procurement import rounds
from backend.app.portal_procurement.config import settings
from core.models import Vendor
from core.time_utils import now_ist_naive

_OPEN = (m.DISCOVERED, m.ENQUIRY_SCHEDULED, m.AWAITING_VENDOR_REPLIES, m.QUOTES_NORMALIZED, m.MARGIN_HOLD)
_RECENT = (m.WINNER_SELECTED, m.NO_VENDOR, m.COMPLETED, m.CLOSED)

_STATE_LABEL = {
    m.DISCOVERED: "Waiting for next round",
    m.ENQUIRY_SCHEDULED: "Being sent",
    m.AWAITING_VENDOR_REPLIES: "Asking vendors",
    m.QUOTES_NORMALIZED: "Deciding",
    m.WINNER_SELECTED: "Winner chosen",
    m.MARGIN_HOLD: "Held (margin)",
    m.NO_VENDOR: "No vendor",
    m.COMPLETED: "PO done",
    m.CLOSED: "Closed",
}


def _f(value):
    return float(value) if value is not None else None


def _group_codes() -> dict[str, str]:
    try:
        from backend.app.integrations.whatsapp import whapi

        return whapi.vendor_groups() if whapi.enabled() else {}
    except Exception:  # noqa: BLE001
        return {}


def live(session: Session) -> dict:
    from backend.app.advance_orders import quotes
    from backend.app.advance_orders.brands import canonical_brand
    from backend.app.advance_orders.service import vendors_for_brand
    from backend.app.portal_procurement.service import _order_lines_for, margin_percent

    now = now_ist_naive()
    since = now - timedelta(days=3)
    demands = session.execute(
        select(m.PortalDemandLine).where(or_(
            m.PortalDemandLine.state.in_(_OPEN),
            (m.PortalDemandLine.state.in_(_RECENT)) & (m.PortalDemandLine.updated_at >= since),
        )).order_by(m.PortalDemandLine.order_date.desc().nulls_last(), m.PortalDemandLine.id.desc())
    ).scalars().all()
    names = dict(session.execute(select(Vendor.id, Vendor.name)).all())
    groups = _group_codes()
    pilot = None if settings.live() else settings.pilot()

    lines_out = []
    open_by_brand: dict[str, int] = {}
    for d in demands:
        brand = canonical_brand(d.brand) or "-"
        if d.state in _OPEN:
            open_by_brand[brand] = open_by_brand.get(brand, 0) + 1
        engine_lines = _order_lines_for(d, session)
        asked: set[int] = set()
        replied: set[int] = set()
        asks_used = 0
        best = None
        if engine_lines:
            ids = {ln.id for ln in engine_lines}
            for q in session.execute(
                select(am.AdvanceVendorQuery).where(am.AdvanceVendorQuery.advance_order_id == d.advance_order_id)
            ).scalars():
                if ids & set(q.line_ids or []) and q.status != am.Q_QUEUED:
                    asked.add(q.vendor_id)
                    if q.status == am.Q_SENT:
                        asks_used = max(asks_used, (q.followups_sent or 0) + 1)
            for ln in engine_lines:
                for x in quotes.for_line(ln.id, session):
                    replied.add(x.vendor_id)
            ranked = quotes.ranked_for_line(engine_lines[0], session)
            if ranked:
                q = ranked[0].quote
                good = [s for s in ranked if rounds.why_not(d, s.quote) is None]
                pick = good[0].quote if good else q
                best = {
                    "vendor": names.get(pick.vendor_id, str(pick.vendor_id)), "tat": pick.tat_days,
                    "discount": _f(pick.discount_pct), "mrp": _f(pick.mrp),
                    "margin": _f(margin_percent(d, pick)), "can_win": bool(good),
                    "why_not": None if good else rounds.why_not(d, q),
                }
        winner = None
        alloc = session.execute(
            select(m.PortalAllocation).where(m.PortalAllocation.demand_line_id == d.id)
            .order_by(m.PortalAllocation.id.desc()).limit(1)
        ).scalar_one_or_none()
        if alloc is not None:
            winner = {"vendor": names.get(alloc.vendor_id, str(alloc.vendor_id)), "tat": alloc.quoted_tat_days,
                      "discount": _f(alloc.quoted_discount_percent), "state": alloc.state}
        lines_out.append({
            "id": d.id, "portal_line": d.source_order_line_id, "po_group": d.source_po_group_id,
            "part_no": d.part_no, "brand": brand, "qty": d.planned_quantity,
            "order_date": d.order_date.isoformat() if d.order_date else None,
            "customer_discount": _f(d.customer_discount_pct), "sale_price": _f(d.sale_price), "mrp": _f(d.portal_mrp),
            "state": d.state, "state_label": _STATE_LABEL.get(d.state, d.state), "note": d.note,
            "in_pilot": pilot is None or (d.source_order_line_id not in pilot["skip_lines"] and bool(
                d.source_order_line_id in pilot["lines"] or rounds._norm(d.part_no) in pilot["parts"]
                or brand in pilot["brands"])),
            "asked": len(asked), "replied": len(replied), "asks_used": asks_used, "max_asks": settings.max_asks,
            "best": best, "winner": winner,
            "choice_pending": bool(d.choice_options) and d.choice_decision is None,
            "choice_code": f"C{d.id}" if d.choice_options else None,
        })

    brands_out = []
    for brand, count in sorted(open_by_brand.items(), key=lambda x: (-x[1], x[0])):
        vendors = vendors_for_brand(brand, session) if brand != "-" else []
        with_group = [v for v in vendors if groups.get((v.vendor_code or "").strip().upper())]
        brands_out.append({
            "brand": brand, "open_lines": count, "vendors": len(vendors), "in_group": len(with_group),
            "in_group_names": [v.name for v in with_group],
            "no_group_names": [v.name for v in vendors if v not in with_group],
        })

    record = rounds._vendor_record(session)
    vendors_out = []
    for vid, rec in record.items():
        if not rec["delivered"] and not rec["asked"]:
            continue
        vendors_out.append({
            "vendor": names.get(vid, str(vid)), "delivered": rec["delivered"],
            "on_time_pct": round(100 * rec["on_time"] / rec["delivered"]) if rec["delivered"] else None,
            "asked": rec["asked"], "reply_pct": round(100 * rec["replied"] / rec["asked"]) if rec["asked"] else None,
        })
    vendors_out.sort(key=lambda v: (-(v["delivered"]), -(v["asked"])))

    last = rounds.last_round(session)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "mode": settings.mode,
        "pilot": None if pilot is None else {
            "brands": sorted(pilot["brands"]), "parts": sorted(pilot["parts"]), "lines": sorted(pilot["lines"]),
            "vendors": [names.get(v, str(v)) for v in sorted(pilot["vendors"])],
        },
        "rules": {
            "poll_minutes": settings.poll_minutes, "round_hours": settings.round_hours,
            "max_asks": settings.max_asks, "max_tat_days": settings.max_tat_days,
            "min_margin": settings.min_margin_percent, "rounds_on": settings.rounds,
            "round_times": [s.strftime("%H:%M") for s in rounds.slots_for(now.date())],
            "sheet_tat_days": settings.sheet_tat_days,
        },
        "last_round": last.slot_at.isoformat(timespec="minutes") if last else None,
        "next_round": rounds.next_slot(now).isoformat(timespec="minutes"),
        "brands": brands_out,
        "lines": lines_out,
        "vendors": vendors_out,
    }
