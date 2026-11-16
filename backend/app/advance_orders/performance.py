"""How well each vendor answers us, for the Vendor Priority dashboard
(Founder, 10 Oct 2026: "show the best vendor according to their performance,
like vendors with least reply time").

Two sources, so a vendor with no advance-order history still gets measured:
  - the WhatsApp chat log: every message the bot sent to one of his numbers
    (stock request, enquiry, reminder) and how long until he next wrote back
    -> reply rate and median reply time;
  - advance-order answers: how often he said YES, and the delivery days he
    promised.
Plus the standing discount for the brand.

score 0-100 = 35 reply rate + 30 speed + 20 yes rate + 15 discount.
Too little history (fewer than MIN_MESSAGES asked) counts as average on the
missing parts, flagged `little_data`, so a new vendor is neither buried nor
crowned on one lucky reply."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from core.models import Vendor

MIN_MESSAGES = 3
REPLY_WINDOW = timedelta(hours=24)
LOOKBACK = timedelta(days=60)


def _vendor_numbers(session: Session) -> dict[str, set[int]]:
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from backend.app.integrations.whatsapp.registry import normalize_number

    out: dict[str, set[int]] = defaultdict(set)
    for number, vendor_id in session.execute(
        select(WhatsAppRegisteredNumber.whatsapp_number, WhatsAppRegisteredNumber.vendor_id)
    ):
        if vendor_id and number:
            out[normalize_number(number)].add(vendor_id)
    for number, vendor_id in session.execute(
        select(m.AdvanceVendorContact.whatsapp_number, m.AdvanceVendorContact.vendor_id).where(
            m.AdvanceVendorContact.active.is_(True)
        )
    ):
        if number:
            out[normalize_number(number)].add(vendor_id)
    return out


def _reply_stats(session: Session) -> dict[int, dict]:
    """{vendor_id: {asked, replied, minutes: [...]}} from the chat log."""
    from backend.app.integrations.whatsapp.models import WhatsAppChatMessage
    from core.time_utils import now_ist_naive

    numbers = _vendor_numbers(session)
    if not numbers:
        return {}
    rows = session.execute(
        select(WhatsAppChatMessage.whatsapp_number, WhatsAppChatMessage.direction, WhatsAppChatMessage.created_at)
        .where(
            WhatsAppChatMessage.whatsapp_number.in_(list(numbers)),
            WhatsAppChatMessage.created_at >= now_ist_naive() - LOOKBACK,
        )
        .order_by(WhatsAppChatMessage.whatsapp_number, WhatsAppChatMessage.created_at)
    ).all()
    per_number: dict[str, dict] = defaultdict(lambda: {"asked": 0, "replied": 0, "minutes": []})
    waiting_since: dict[str, object] = {}
    for number, direction, at in rows:
        stats = per_number[number]
        if direction == "out":
            # Several messages in a row before he answers = ONE ask; his reply
            # time counts from the first of them.
            if number not in waiting_since:
                waiting_since[number] = at
                stats["asked"] += 1
        elif number in waiting_since:
            started = waiting_since.pop(number)
            if at - started <= REPLY_WINDOW:
                stats["replied"] += 1
                stats["minutes"].append((at - started).total_seconds() / 60)
    out: dict[int, dict] = defaultdict(lambda: {"asked": 0, "replied": 0, "minutes": []})
    for number, stats in per_number.items():
        for vendor_id in numbers.get(number, ()):
            out[vendor_id]["asked"] += stats["asked"]
            out[vendor_id]["replied"] += stats["replied"]
            out[vendor_id]["minutes"].extend(stats["minutes"])
    return out


def _answer_stats(session: Session) -> dict[int, dict]:
    out: dict[int, dict] = defaultdict(lambda: {"answers": 0, "yes": 0, "tat": []})
    for vendor_id, available, tat in session.execute(
        select(m.AdvanceVendorQuote.vendor_id, m.AdvanceVendorQuote.available, m.AdvanceVendorQuote.tat_days)
    ):
        stats = out[vendor_id]
        stats["answers"] += 1
        if available:
            stats["yes"] += 1
            if tat is not None:
                stats["tat"].append(tat)
    return out


def vendor_performance(session: Session) -> dict:
    """{"vendors": {vendor_id: metrics}, "brands": {brand: [vendor_id best-first]}}."""
    replies = _reply_stats(session)
    answers = _answer_stats(session)
    names = dict(session.execute(select(Vendor.id, Vendor.name)).all())

    brand_rows: dict[str, list[m.VendorBrand]] = defaultdict(list)
    for vb in session.execute(select(m.VendorBrand).where(m.VendorBrand.active.is_(True))).scalars():
        brand_rows[vb.brand].append(vb)

    def base(vendor_id: int) -> dict:
        r = replies.get(vendor_id, {"asked": 0, "replied": 0, "minutes": []})
        a = answers.get(vendor_id, {"answers": 0, "yes": 0, "tat": []})
        enough = r["asked"] >= MIN_MESSAGES
        reply_rate = r["replied"] / r["asked"] if r["asked"] else None
        med = median(r["minutes"]) if r["minutes"] else None
        yes_rate = a["yes"] / a["answers"] if a["answers"] else None
        return {
            "vendor_id": vendor_id,
            "vendor_name": names.get(vendor_id, str(vendor_id)),
            "asked": r["asked"],
            "replied": r["replied"],
            "reply_rate": round(reply_rate * 100) if reply_rate is not None else None,
            "median_reply_minutes": round(med) if med is not None else None,
            "answers": a["answers"],
            "yes_rate": round(yes_rate * 100) if yes_rate is not None else None,
            "avg_tat_days": round(sum(a["tat"]) / len(a["tat"]), 1) if a["tat"] else None,
            "little_data": not enough,
            # parts of the score, 0..1; average (0.5) where unknown
            "_reply": reply_rate if enough and reply_rate is not None else 0.5,
            "_speed": (1 / (1 + med / 60)) if enough and med is not None else 0.5,
            "_yes": yes_rate if yes_rate is not None else 0.5,
        }

    vendors: dict[int, dict] = {}
    brands: dict[str, list[dict]] = {}
    for brand, rows in brand_rows.items():
        pcts = [float(vb.discount_pct) for vb in rows if vb.discount_type == m.DISC_PERCENT and vb.discount_pct is not None]
        top_pct = max(pcts) if pcts else 0
        scored = []
        for vb in rows:
            if vb.vendor_id not in vendors:
                vendors[vb.vendor_id] = base(vb.vendor_id)
            v = vendors[vb.vendor_id]
            if vb.discount_type == m.DISC_PERCENT and vb.discount_pct is not None and top_pct:
                disc = float(vb.discount_pct) / top_pct
            else:
                disc = 0.5
            score = round(100 * (0.35 * v["_reply"] + 0.30 * v["_speed"] + 0.20 * v["_yes"] + 0.15 * disc))
            scored.append({"vendor_id": vb.vendor_id, "score": score})
        scored.sort(key=lambda x: (-x["score"], vendors[x["vendor_id"]]["vendor_name"]))
        brands[brand] = scored
    for v in vendors.values():
        for key in ("_reply", "_speed", "_yes"):
            v.pop(key, None)
    return {"vendors": vendors, "brands": brands}
