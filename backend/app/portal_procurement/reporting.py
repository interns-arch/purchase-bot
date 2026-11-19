"""Excel files and the end-of-day report (19 Nov 2026 procurement meeting).

* Parts to vendors as a file: the 18 Nov enquiry CSV does that
  (advance_orders/enquiry_csv.py), so nothing here sends to vendors.
* `collect` / `report_text` / `report_excel`: what came in today, what was
  bought, what failed and which vendors were asked, brand by brand, sent to
  Prateek sir in the evening (PORTAL_PROCUREMENT_REPORT_TIME, default 20:00).
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as am
from backend.app.portal_procurement import models as m
from backend.app.portal_procurement.config import SOURCE, settings
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_OPEN = (m.DISCOVERED, m.ENQUIRY_SCHEDULED, m.AWAITING_VENDOR_REPLIES, m.QUOTES_NORMALIZED, m.WINNER_SELECTED)
_FAILED = (m.NO_VENDOR, m.MARGIN_HOLD)


# ====================================================================== Excel
def _workbook(sheets: dict[str, tuple[list[str], list[list]]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    wb.remove(wb.active)
    for title, (header, rows) in sheets.items():
        ws = wb.create_sheet(title[:31])
        ws.append(header)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append(row)
        for column in ws.columns:
            width = max(len(str(c.value)) if c.value is not None else 0 for c in column)
            ws.column_dimensions[column[0].column_letter].width = min(max(10, width + 2), 60)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ====================================================================== the evening report
def _name(vendor_id, session: Session) -> str:
    from backend.app.portal_procurement.service import _vendor_name

    return _vendor_name(vendor_id, session)


def _on(value: datetime | None, day: date) -> bool:
    return value is not None and value.date() == day


def collect(session: Session, day: date | None = None) -> dict:
    day = day or now_ist_naive().date()
    demands = session.execute(select(m.PortalDemandLine)).scalars().all()
    touched = [d for d in demands if _on(d.first_seen_at, day) or _on(d.updated_at, day)]
    allocs = [a for a in session.execute(select(m.PortalAllocation)).scalars().all()
              if _on(a.created_at, day) and a.state not in (m.ALLOCATION_FAILED, m.ALLOCATION_CANCELLED)]
    by_id = {d.id: d for d in demands}

    brands: dict[str, dict] = defaultdict(lambda: {"received": 0, "bought": 0, "failed": 0, "open": 0,
                                                  "vendors_asked": set(), "vendors_replied": set()})
    for d in touched:
        row = brands[d.brand or "-"]
        if _on(d.first_seen_at, day):
            row["received"] += 1
        if d.state in _OPEN:
            row["open"] += 1
        elif d.state in _FAILED or (d.state == m.CLOSED and d.choice_decision == "cancel"):
            if _on(d.updated_at, day):
                row["failed"] += 1
    bought_lines = set()
    for a in allocs:
        d = by_id.get(a.demand_line_id)
        if d is not None:
            brands[d.brand or "-"]["bought"] += 1
            bought_lines.add(d.id)

    vendors: dict[int, dict] = {}

    def vendor(vid):
        return vendors.setdefault(vid, {"asked": 0, "replied": 0, "bought": 0, "disc": [], "days": []})

    for q, order in session.execute(
        select(am.AdvanceVendorQuery, am.AdvanceOrder)
        .join(am.AdvanceOrder, am.AdvanceOrder.id == am.AdvanceVendorQuery.advance_order_id)
        .where(am.AdvanceOrder.source == SOURCE)
    ).all():
        if not _on(q.sent_at, day):
            continue
        v = vendor(q.vendor_id)
        v["asked"] += 1
        brands[q.brand or "-"]["vendors_asked"].add(q.vendor_id)
        if q.status == am.Q_REPLIED:
            v["replied"] += 1
            brands[q.brand or "-"]["vendors_replied"].add(q.vendor_id)
    for a in allocs:
        if a.vendor_id is None:
            continue
        v = vendor(a.vendor_id)
        v["bought"] += 1
        if a.quoted_discount_percent is not None:
            v["disc"].append(float(a.quoted_discount_percent))
        if a.quoted_tat_days is not None:
            v["days"].append(a.quoted_tat_days)

    parts = []
    alloc_by_demand = {a.demand_line_id: a for a in allocs}
    for d in sorted(touched, key=lambda x: (x.brand or "", x.id)):
        a = alloc_by_demand.get(d.id)
        if d.id in bought_lines and a is not None:
            status = "BOUGHT"
        elif d.state in _FAILED or (d.state == m.CLOSED and d.choice_decision == "cancel"):
            status = "FAILED"
        elif d.state in _OPEN:
            status = "OPEN"
        else:
            status = d.state
        parts.append({
            "part": d.part_no, "brand": d.brand or "-", "qty": d.planned_quantity, "status": status,
            "vendor": _name(a.vendor_id, session) if a is not None and a.vendor_id else "",
            "discount": float(a.quoted_discount_percent) if a is not None and a.quoted_discount_percent is not None else None,
            "days": a.quoted_tat_days if a is not None else None,
            "note": (d.note or "")[:200],
        })
    out_brands = {
        b: {**{k: v for k, v in r.items() if not isinstance(v, set)},
            "vendors_asked": len(r["vendors_asked"]), "vendors_replied": len(r["vendors_replied"])}
        for b, r in sorted(brands.items())
    }
    out_vendors = [
        {"vendor": _name(vid, session), "asked": v["asked"], "replied": v["replied"], "bought": v["bought"],
         "avg_discount": round(sum(v["disc"]) / len(v["disc"]), 1) if v["disc"] else None,
         "avg_days": round(sum(v["days"]) / len(v["days"]), 1) if v["days"] else None}
        for vid, v in sorted(vendors.items(), key=lambda kv: -kv[1]["asked"])
    ]
    totals = {
        "received": sum(r["received"] for r in out_brands.values()),
        "bought": sum(r["bought"] for r in out_brands.values()),
        "failed": sum(r["failed"] for r in out_brands.values()),
        "open": sum(r["open"] for r in out_brands.values()),
    }
    return {"day": day, "totals": totals, "brands": out_brands, "vendors": out_vendors, "parts": parts}


def report_text(data: dict, *, excel: bool = False) -> str:
    t = data["totals"]
    lines = [
        f"📊 Purchase report — {data['day']:%d %b %Y}",
        f"Aaj aaye: {t['received']} · Khareed hui (PO-MST): {t['bought']} · Fail: {t['failed']} · Abhi baaki: {t['open']}",
    ]
    if data["brands"]:
        lines.append("\nBrand wise:")
        for brand, r in list(data["brands"].items())[:10]:
            lines.append(f"• {brand}: aaye {r['received']}, hue {r['bought']}, fail {r['failed']}, baaki {r['open']} "
                         f"— {r['vendors_asked']} vendor se poocha, {r['vendors_replied']} ne jawab diya")
        if len(data["brands"]) > 10:
            lines.append(f"… aur {len(data['brands']) - 10} brand (poori list Excel mein)")
    if data["vendors"]:
        lines.append("\nVendor wise:")
        for v in data["vendors"][:10]:
            extra = ""
            if v["bought"]:
                bits = []
                if v["avg_discount"] is not None:
                    bits.append(f"avg {v['avg_discount']:g}%")
                if v["avg_days"] is not None:
                    bits.append(f"{v['avg_days']:g} din")
                extra = f", {v['bought']} mile" + (f" ({', '.join(bits)})" if bits else "")
            lines.append(f"• {v['vendor']}: poocha {v['asked']}, jawab {v['replied']}{extra}")
    failed = [p for p in data["parts"] if p["status"] == "FAILED"]
    if failed:
        lines.append("\nJo parts nahi mile:")
        for p in failed[:10]:
            lines.append(f"• {p['part']} ({p['brand']}) x{p['qty']}" + (f" — {p['note']}" if p["note"] else ""))
        if len(failed) > 10:
            lines.append(f"… aur {len(failed) - 10} part")
    if not data["parts"]:
        lines.append("\nAaj koi Portal part nahi aaya.")
    if excel:
        lines.append("\nPoori detail Excel mein bheji hai.")
    return "\n".join(lines)


def report_excel(data: dict) -> bytes:
    return _workbook({
        "Brands": (["Brand", "Received", "Bought", "Failed", "Open", "Vendors asked", "Vendors replied"],
                   [[b, r["received"], r["bought"], r["failed"], r["open"], r["vendors_asked"], r["vendors_replied"]]
                    for b, r in data["brands"].items()]),
        "Vendors": (["Vendor", "Asked", "Replied", "Bought", "Avg discount %", "Avg days"],
                    [[v["vendor"], v["asked"], v["replied"], v["bought"], v["avg_discount"], v["avg_days"]]
                     for v in data["vendors"]]),
        "Parts": (["Part No", "Brand", "Qty", "Status", "Vendor", "Discount %", "Days", "Note"],
                  [[p["part"], p["brand"], p["qty"], p["status"], p["vendor"], p["discount"], p["days"], p["note"]]
                   for p in data["parts"]]),
    })


def _send_file(number: str, content: bytes, filename: str, caption: str) -> bool:
    try:
        from backend.app.integrations.whatsapp import outbound
        from backend.app.integrations.whatsapp.client import WhatsAppClient
        from backend.app.integrations.whatsapp.config import whatsapp_settings

        if not outbound.in_service_window(number):
            return False  # outside the 24 h window a file cannot be sent; the text already went
        client = WhatsAppClient(whatsapp_settings)
        media_id = client.upload_media(content, filename, XLSX)
        client.send_document_message(number, media_id, filename, caption=caption)
        return True
    except Exception:  # noqa: BLE001 -- the text report already went
        logger.exception("Portal report: Excel to %s failed.", number)
        return False


def send_report(session: Session, day: date | None = None) -> bool:
    """Text (and the Excel when it can be sent) to Prateek sir. True when sent."""
    from backend.app.portal_procurement import rounds

    numbers = rounds.prateek_numbers()
    if not numbers:
        logger.warning("Portal report: nobody to send it to (ADVANCE_ORDER_ESCALATION_NUMBERS is empty).")
        return False
    data = collect(session, day)
    excel = settings.report_excel
    rounds._send(report_text(data, excel=excel))
    if excel:
        content = report_excel(data)
        name = f"Purchase_report_{data['day']:%Y-%m-%d}.xlsx"
        for number in numbers:
            _send_file(number, content, name, f"Purchase report {data['day']:%d %b %Y}")
    logger.info("Portal report for %s sent: %s", data["day"], data["totals"])
    return True


def report_job() -> None:
    """Scheduler: once a day at PORTAL_PROCUREMENT_REPORT_TIME."""
    from core.db import get_session

    if not settings.enabled:
        return
    with get_session() as session:
        send_report(session)
