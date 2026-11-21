"""3-hour rounds for Portal procurement (User, 16 Nov 2026).

* The open Active Procurement lines go to the vendors once every
  `round_hours` inside vendor hours (09:30, 12:30, 15:30, 18:30), each vendor
  only the parts of his own brands, and only in his WhatsApp group.
* A part is asked at most `max_asks` times per vendor (first ask + reminders
  one round apart). Silent after that: Prateek sir is told, and that vendor
  is not asked about that part again.
* Only a reply with at most `max_tat_days` days AND the minimum margin over
  the customer's price can win. The best one wins at the end of a round
  (fewer days first, then the bigger discount, then fewer exact days, then
  the vendor who delivers on time -- or, with no delivery record yet, the one
  who replies more often).
* Nothing good enough and nobody left to ask, but replies on the table:
  Prateek sir gets the numbered list and replies "C78 2" or "C78 cancel".
* Delivery record: the promised date is kept when the PO-MST is made, and the
  day the Portal first shows the parts received.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as am
from backend.app.advance_orders.config import advance_order_settings as acfg
from backend.app.portal_procurement import models as m
from backend.app.portal_procurement.config import settings
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)


# ====================================================================== timing
def _minutes(value: str, default: int) -> int:
    try:
        h, mnt = (int(x) for x in value.strip().split(":"))
        return h * 60 + mnt
    except (ValueError, AttributeError):
        return default


def _hours() -> tuple[int, int]:
    """Round window as minutes after midnight: (first round, last round)."""
    return _minutes(settings.round_start, 10 * 60), _minutes(settings.round_end, 19 * 60)


def slots_for(day: date) -> list[datetime]:
    """10:00, 13:00, 16:00, 19:00 by default -- the last one included."""
    start, end = _hours()
    base = datetime(day.year, day.month, day.day)
    out, at = [], start
    while at <= end:
        out.append(base + timedelta(minutes=at))
        at += settings.round_hours * 60
    return out


def current_slot(now: datetime) -> datetime | None:
    """The round due now: the latest round time at or before `now` today,
    while its 3 hours have not run out (a round missed overnight is not run)."""
    past = [s for s in slots_for(now.date()) if s <= now < s + timedelta(hours=settings.round_hours)]
    return past[-1] if past else None


def next_slot(now: datetime) -> datetime:
    for day in range(0, 8):
        for slot in slots_for(now.date() + timedelta(days=day)):
            if slot > now:
                return slot
    return now + timedelta(hours=settings.round_hours)


def round_due(session: Session, now: datetime | None = None) -> datetime | None:
    """The round to run now, or None (outside hours / already run)."""
    now = now or now_ist_naive()
    slot = current_slot(now)
    if slot is None:
        return None
    done = session.execute(select(m.PortalRound.id).where(m.PortalRound.slot_at == slot)).first()
    return None if done else slot


def mark_round(session: Session, slot: datetime, sent: int) -> None:
    session.add(m.PortalRound(slot_at=slot, lines_sent=sent))
    session.flush()


def last_round(session: Session) -> m.PortalRound | None:
    return session.execute(select(m.PortalRound).order_by(m.PortalRound.slot_at.desc()).limit(1)).scalar_one_or_none()


def ask_plan() -> tuple[int, int, int]:
    """(vendor wait minutes, reminder count, minutes between asks) for Portal orders."""
    gap = settings.round_hours * 60
    return gap * settings.max_asks, settings.max_asks - 1, gap


# ====================================================================== who may win
def _norm(value) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def demand_for_line(line: am.AdvanceOrderLine, session: Session) -> m.PortalDemandLine | None:
    if not line.advance_order_id:
        return None
    key = _norm(line.part_number)
    for demand in session.execute(
        select(m.PortalDemandLine).where(m.PortalDemandLine.advance_order_id == line.advance_order_id)
    ).scalars():
        if _norm(demand.part_no) == key:
            return demand
    return None


def why_not(demand: m.PortalDemandLine | None, quote) -> str | None:
    """None = this quote may win; else the reason it may not."""
    if not quote.available:
        return "not available"
    if quote.tat_days is None:
        return "no days given"
    if quote.tat_days > settings.max_tat_days:
        return f"{quote.tat_days} days (over {settings.max_tat_days})"
    floor = Decimal(str(settings.min_margin_percent or 0))
    if demand is None or floor <= 0:
        return None
    from backend.app.portal_procurement.service import _customer_known, margin_percent

    if not _customer_known(demand):
        return None  # customer price unknown -> margin not checked
    mg = margin_percent(demand, quote)
    if mg is None:
        return "margin unknown (MRP or discount missing)"
    if mg < floor:
        return f"margin {mg}% (under {floor:g}%)"
    return None


def _vendor_record(session: Session) -> dict[int, dict]:
    """{vendor_id: {"delivered", "on_time", "asked", "replied"}} -- delivery
    from Portal allocations, replies from every vendor question the bot sent."""
    out: dict[int, dict] = {}

    def slot(vid):
        return out.setdefault(vid, {"delivered": 0, "on_time": 0, "asked": 0, "replied": 0, "late": 0})

    for vid, promised, received in session.execute(
        select(m.PortalAllocation.vendor_id, m.PortalAllocation.promised_date, m.PortalAllocation.received_at)
        .where(m.PortalAllocation.received_at.is_not(None), m.PortalAllocation.vendor_id.is_not(None))
    ):
        rec = slot(vid)
        rec["delivered"] += 1
        if promised is None or received.date() <= promised:
            rec["on_time"] += 1
    for vid, status, draft in session.execute(
        select(am.AdvanceVendorQuery.vendor_id, am.AdvanceVendorQuery.status, am.AdvanceVendorQuery.draft)
        .where(am.AdvanceVendorQuery.status.in_((am.Q_REPLIED, am.Q_TIMEOUT, am.Q_SENT)))
    ):
        rec = slot(vid)
        rec["asked"] += 1
        if status == am.Q_REPLIED:
            rec["replied"] += 1
        elif isinstance(draft, dict) and draft.get("_late"):
            rec["late"] += 1  # answered after his batch window (21 Nov 2026)
    return out


def history_key(rec: dict | None) -> tuple:
    """Lower is better: on-time delivery when known, else reply record."""
    rec = rec or {}
    if rec.get("delivered"):
        return (0, -rec["on_time"] / rec["delivered"], -rec["delivered"])
    if rec.get("asked"):
        return (1, -rec["replied"] / rec["asked"], 0)
    return (2, 0, 0)


def fill_ties(scored: list, session: Session) -> None:
    """Portal lines: same speed band and same discount -> fewer exact days,
    then the better delivery / reply record."""
    record: dict | None = None
    groups: dict[tuple, list] = {}
    order = settings.priority_vendors()
    if order:
        # The optional file (one order for every brand) wins when it is set.
        pos = {vid: i for i, vid in enumerate(order)}
        for s in scored:
            s.priority_rank = pos.get(s.quote.vendor_id, len(order))
    else:
        # User, 20 Nov 2026: the brand-wise order set on the dashboard's
        # Vendors page (vendor_brands.priority, 1 = first) beats a bigger
        # discount inside the same speed band. Vendors left at the same
        # position still go by discount; not on the brand's list = last.
        for s in scored:
            s.priority_rank = s.vendor_priority
    for s in scored:
        s.tie = (s.quote.tat_days if s.quote.tat_days is not None else 999,)
        groups.setdefault((s.band, s.terms_tier, s.discount_pct, s.tie), []).append(s)
    for members in groups.values():
        if len(members) < 2:
            continue
        if record is None:
            record = _vendor_record(session)
        for s in members:
            s.tie = s.tie + history_key(record.get(s.quote.vendor_id))


def decide_line(line: am.AdvanceOrderLine, covering: list, still_open: list, now: datetime,
                session: Session) -> bool:
    """The Portal rule for one asking line. True when the line was decided
    (a winner, or handed to Prateek sir)."""
    from backend.app.advance_orders import quotes

    demand = demand_for_line(line, session)
    ranked = quotes.ranked_for_line(line, session)
    good = [s for s in ranked if why_not(demand, s.quote) is None]
    sent = [q.sent_at for q in covering if q.sent_at is not None]
    round_over = bool(sent) and min(sent) + timedelta(hours=settings.round_hours) <= now
    if good and (not still_open or round_over):
        return quotes.apply_winner(line, session, allowed={s.quote.id for s in good})
    if still_open or not ranked:
        return False  # keep asking; or nothing on the table (the engine handles "nobody")
    if demand is None:
        return quotes.apply_winner(line, session)
    offer_choice(session, demand, line, ranked)
    return True


# ====================================================================== Prateek sir
def prateek_numbers() -> list[str]:
    from backend.app.advance_orders.service import _normalize

    return [n for n in dict.fromkeys(_normalize(x) for x in acfg.escalation_numbers or []) if n]


def _send(text: str) -> None:
    from backend.app.advance_orders.service import _send_text

    for number in prateek_numbers():
        try:
            _send_text(number, text)
        except Exception:  # noqa: BLE001 -- the next number is tried
            logger.exception("Portal rounds: message to %s failed.", number)


def _vendor(vendor_id, session: Session) -> str:
    from backend.app.portal_procurement.service import _vendor_name

    return _vendor_name(vendor_id, session)


def _money(value) -> str:
    return f"{Decimal(value):.2f}".rstrip("0").rstrip(".") if value is not None else "-"


def offer_choice(session: Session, demand: m.PortalDemandLine, line: am.AdvanceOrderLine, ranked: list) -> None:
    from backend.app.portal_procurement.service import margin_percent

    options = []
    for n, s in enumerate(ranked[:9], start=1):
        q = s.quote
        mg = margin_percent(demand, q)
        options.append({
            "n": n, "quote_id": q.id, "vendor_id": q.vendor_id, "vendor": _vendor(q.vendor_id, session),
            "tat": q.tat_days, "discount": float(q.discount_pct) if q.discount_pct is not None else None,
            "mrp": float(q.mrp) if q.mrp is not None else None,
            "margin": float(mg) if mg is not None else None, "why": why_not(demand, q),
        })
    line.status = am.LINE_ESCALATED
    line.note = "waiting for Prateek sir to choose"
    demand.choice_options = options
    demand.choice_sent_at = now_ist_naive()
    demand.choice_decision = None
    session.flush()
    cust = f"{demand.customer_discount_pct:g}% off" if demand.customer_discount_pct is not None else "discount -"
    rows = []
    for o in options:
        days = f"{o['tat']} din" if o["tat"] is not None else "din -"
        disc = f"{o['discount']:g}% off" if o["discount"] is not None else "discount -"
        mg = f"margin {o['margin']:g}%" if o["margin"] is not None else "margin -"
        rows.append(f"{o['n']}. {o['vendor']} — {days}, {disc}, {mg}")
    code = f"C{demand.id}"
    _send(
        f"🟠 Vendor choose kijiye — {code}\n"
        f"{demand.part_no} x{demand.planned_quantity} ({demand.brand or '-'})\n"
        f"Customer: {cust}, pays Rs {_money(demand.sale_price)} (MRP Rs {_money(demand.portal_mrp)})\n"
        f"Kisi reply mein {settings.min_margin_percent:g}% margin aur {settings.max_tat_days} din dono nahi mile:\n"
        + "\n".join(rows)
        + f"\n\nReply: \"{code} 1\" (us vendor se PO-MST) ya \"{code} cancel\"."
    )
    logger.info("Portal line %s: no reply good enough -- Prateek sir asked to choose (%s).",
                demand.source_order_line_id, code)


_CHOICE = re.compile(r"^\s*(?:c\s*-?\s*(\d+)\s*[:\-.]?\s*)?(\d{1,2}|cancel|radd|rad|nahi|no)\s*\.?\s*$", re.I)


def _pending(session: Session) -> list[m.PortalDemandLine]:
    return list(session.execute(
        select(m.PortalDemandLine).where(
            m.PortalDemandLine.choice_options.is_not(None), m.PortalDemandLine.choice_decision.is_(None)
        ).order_by(m.PortalDemandLine.id)
    ).scalars())


def handle_reply(sender: str, text: str, session: Session) -> bool:
    """Prateek sir answering a vendor choice. True when it was one."""
    from backend.app.advance_orders.service import _normalize, _send_text, _settle

    number = _normalize(sender)
    if not number or number not in prateek_numbers():
        return False
    match = _CHOICE.match(text or "")
    if not match:
        return False
    pending = _pending(session)
    if not pending:
        return False
    code, answer = match.group(1), match.group(2).lower()
    if code:
        demand = next((d for d in pending if d.id == int(code)), None)
        if demand is None:
            _send_text(number, f"C{code} ka koi choice pending nahi hai.")
            return True
    elif len(pending) == 1:
        demand = pending[0]
    else:
        codes = ", ".join(f"C{d.id} ({d.part_no})" for d in pending)
        _send_text(number, f"Kai choices pending hain: {codes}. Kripya code ke saath likhiye, jaise \"C{pending[0].id} 1\".")
        return True

    lines = [ln for ln in _engine_lines(demand, session) if ln.status == am.LINE_ESCALATED]
    order = session.get(am.AdvanceOrder, demand.advance_order_id) if demand.advance_order_id else None
    if answer in ("cancel", "radd", "rad", "nahi", "no"):
        from backend.app.portal_procurement.service import _move

        for ln in lines:
            ln.status = am.LINE_CANCELLED
            ln.note = "cancelled by Prateek sir"
        demand.choice_decision = "cancel"
        _move(session, demand, m.CLOSED, entity="demand", actor="prateek", note="cancelled by Prateek sir",
              error="cancelled by Prateek sir")
        if order is not None:
            _settle(order)
        session.flush()
        _send_text(number, f"✅ C{demand.id}: {demand.part_no} cancel kar diya. Is part ke liye ab vendor se nahi poochenge.")
        return True

    option = next((o for o in demand.choice_options or [] if o.get("n") == int(answer)), None)
    quote = session.get(am.AdvanceVendorQuote, option["quote_id"]) if option else None
    if quote is None or not lines:
        _send_text(number, f"C{demand.id}: option {answer} nahi mila. 1 se {len(demand.choice_options or [])} tak likhiye, ya \"cancel\".")
        return True
    from backend.app.portal_procurement.service import _point_line_at

    line = next((ln for ln in lines if ln.id == quote.advance_order_line_id), lines[0])
    line.status = am.LINE_AVAILABLE
    _point_line_at(line, quote, f"chosen by Prateek sir: {option['vendor']}")
    demand.choice_decision = f"quote:{quote.id}"
    if order is not None:
        _settle(order)
    session.flush()
    _send_text(number, f"✅ C{demand.id}: {option['vendor']} — PO-MST bana rahe hain ({demand.part_no} x{demand.planned_quantity}).")
    logger.info("Portal line %s: Prateek sir chose quote %s.", demand.source_order_line_id, quote.id)
    return True


def _engine_lines(demand: m.PortalDemandLine, session: Session) -> list[am.AdvanceOrderLine]:
    from backend.app.portal_procurement.service import _order_lines_for

    return _order_lines_for(demand, session)


def tell_silent(session: Session, items: list[tuple[am.AdvanceOrder, am.AdvanceVendorQuery]]) -> None:
    """Vendors who stayed silent through every ask -> one message to Prateek sir."""
    if not items:
        return
    rows = []
    for order, q in items:
        parts = ", ".join(f"{ln.part_number} x{ln.qty}" for ln in order.lines if ln.id in (q.line_ids or []))
        rows.append(f"• {_vendor(q.vendor_id, session)} ({q.brand}): {parts}")
    _send(
        f"🔕 In vendors ne {settings.max_asks} baar poochne par bhi reply nahi kiya:\n"
        + "\n".join(rows)
        + "\n\nIn parts ke liye ab inse nahi poocha jayega."
    )


# ====================================================================== today's stock sheet
def _todays_sheet(vendor_id: int, session: Session, today: date):
    """His active stock import, when it arrived today (or any day, if
    PORTAL_PROCUREMENT_SHEET_TODAY_ONLY=false)."""
    from core.models import InventoryImport

    imp = session.execute(
        select(InventoryImport).where(InventoryImport.vendor_id == vendor_id, InventoryImport.is_active.is_(True))
    ).scalars().first()
    if imp is None:
        return None
    if not settings.sheet_today_only:
        return imp
    at = imp.completed_at or imp.confirmed_at or imp.created_at
    return imp if at is not None and at.date() == today else None


_DAYS_KEY = re.compile(r"\b(tat|lead|days?|din|delivery|eta)\b", re.I)


def _sheet_days(raw: dict | None) -> int | None:
    """Days from a days / TAT / lead-time column of his sheet, if it has one."""
    for key, value in (raw or {}).items():
        if not _DAYS_KEY.search(str(key).replace("_", " ")):
            continue
        found = re.search(r"\d+", str(value if value is not None else ""))
        if found and 0 <= int(found.group()) <= 120:
            return int(found.group())
    return None


def take_from_sheet(order: am.AdvanceOrder, brand: str, vendor_id: int, lines: list[am.AdvanceOrderLine],
                    session: Session, now: datetime) -> list[am.AdvanceOrderLine]:
    """Lines this vendor's stock sheet from today shows he HAS. He is not asked
    whether he has them. What the sheet gives (pieces, MRP -- else the Portal
    MRP --, discount from the sheet price, days from a days column) is his
    answer. When the discount or days is missing he is asked ONLY that, one
    thing at a time (steps.py); a complete sheet is recorded as his reply at
    once. Either way it is ranked with everyone else's at the end of the
    round. Returns the lines the sheet handled (not to be asked again)."""
    from backend.app.advance_orders import quotes, steps, stock_routing
    from backend.app.advance_orders.parser import LineAnswer
    from core.models import VendorInventory

    imp = _todays_sheet(vendor_id, session, now.date())
    if imp is None:
        return []
    found: list[tuple[am.AdvanceOrderLine, dict, str]] = []
    for line in lines:
        holder = next((h for h in stock_routing.stock_holders(line, session) if h.vendor_id == vendor_id), None)
        if holder is None or holder.remaining <= 0:
            continue
        numbers = stock_routing._matchable(line.part_number, session)  # noqa: SLF001
        row = session.execute(
            select(VendorInventory).where(
                VendorInventory.import_id == imp.id, VendorInventory.normalized_part_number.in_(numbers or [""]),
                VendorInventory.quantity_available > 0,
            ).order_by(VendorInventory.quantity_available.desc())
        ).scalars().first()
        if row is None:
            continue
        demand = demand_for_line(line, session)
        mrp = row.mrp if row.mrp and row.mrp > 0 else (demand.portal_mrp if demand is not None else None)
        disc = None
        if row.price and row.mrp and 0 < row.price < row.mrp:
            disc = ((row.mrp - row.price) / row.mrp * 100).quantize(Decimal("0.001"))
        days = _sheet_days(row.raw_data)
        if days is None:
            days = settings.sheet_tat_days
        entry = {
            "available": True, "qty": int(min(holder.remaining, line.qty)),
            "mrp": str(mrp) if mrp is not None else None,
            "disc": str(disc) if disc is not None else None, "tat": days,
        }
        found.append((line, entry, f"stock sheet {imp.file_name}: {holder.remaining:g} free"
                                   + (f", MRP {row.mrp:g}" if row.mrp else "") + (f", price {row.price:g}" if row.price else "")
                                   + (f", {days} din" if days is not None else "")))
    if not found:
        return []
    complete = [x for x in found if steps.next_step({str(x[0].id): x[1]}, [x[0]]) == steps.DONE]
    partial = [x for x in found if x not in complete]
    if complete:
        query = am.AdvanceVendorQuery(
            advance_order_id=order.id, brand=brand, vendor_id=vendor_id, line_ids=[ln.id for ln, _e, _t in complete],
            status=am.Q_REPLIED, sent_at=now, replied_at=now, step="done",
            reply_text="from today's stock sheet -- not asked",
        )
        session.add(query)
        session.flush()
        terms = quotes.brand_terms(vendor_id, brand, session)
        for line, entry, raw in complete:
            quotes.record(order_id=order.id, line=line, vendor_id=vendor_id, answer=steps.answer_for(entry, now.date()),
                          terms=terms, session=session, query_id=query.id, raw_reply=raw, source="stock sheet")
    if partial:
        # Queued like any question, but already past "do you have it": the
        # sheet's answers are the draft, and the first question is the first
        # missing thing (discount, else days). _send_queued sends it.
        draft: dict = {str(ln.id): entry for ln, entry, _t in partial}
        session.add(am.AdvanceVendorQuery(
            advance_order_id=order.id, brand=brand, vendor_id=vendor_id, line_ids=[ln.id for ln, _e, _t in partial],
            status=am.Q_QUEUED, step=steps.next_step(draft, [ln for ln, _e, _t in partial]),
            draft={**draft, "_sheet": "; ".join(t for _l, _e, t in partial)[:900]},
        ))
    session.flush()
    logger.info("Advance order %s: vendor %s not asked if he has %d part(s) -- today's stock sheet shows them "
                "(%d complete, %d asked only the missing discount / days).",
                order.id, vendor_id, len(found), len(complete), len(partial))
    return [ln for ln, _e, _t in found]


# ====================================================================== delivery record
def promise_date(alloc: m.PortalAllocation, arrival: str | None) -> date | None:
    if arrival:
        try:
            return date.fromisoformat(str(arrival)[:10])
        except ValueError:
            pass
    if alloc.quoted_tat_days is not None:
        return now_ist_naive().date() + timedelta(days=int(alloc.quoted_tat_days))
    return None


def note_receipts(session: Session, received: dict[int, int]) -> int:
    """{source_order_line_id: qty_received} from the Portal rows -> the first
    day each allocated line showed as received."""
    if not received:
        return 0
    done = 0
    for alloc, sol in session.execute(
        select(m.PortalAllocation, m.PortalDemandLine.source_order_line_id)
        .join(m.PortalDemandLine, m.PortalDemandLine.id == m.PortalAllocation.demand_line_id)
        .where(m.PortalAllocation.received_at.is_(None), m.PortalAllocation.promised_date.is_not(None),
               m.PortalDemandLine.source_order_line_id.in_(list(received)))
    ).all():
        if received.get(sol, 0) > 0:
            alloc.received_at = now_ist_naive()
            done += 1
    if done:
        session.flush()
        logger.info("Portal procurement: %d delivery(ies) recorded as received.", done)
    return done


# ====================================================================== known discount (19 Nov 2026)
# Meeting rule: a vendor whose discount for the brand is already set on the
# Dealer Portal is NOT asked for a discount; a first-time vendor with none on
# file is. The Portal row for a line (`GET /procurement/active/{id}/vendors`)
# carries `discount_percent` per dealer. It is read while the round is
# prepared (poll_job has the Portal client) and kept in memory for a few
# hours -- the vendor question itself never calls the Portal. 0 / empty means
# "nothing set", so he is asked.
_dealer_discounts: dict[int, tuple[datetime, dict[int, Decimal]]] = {}


def _pct(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 -- not a number
        return None
    return number if 0 < number <= 100 else None


def remember_dealer_discounts(source_line_id: int, rows: list[dict], now: datetime | None = None) -> dict[int, Decimal]:
    """{dealer_id: discount %} from a Portal vendor-options list, kept for this line."""
    from backend.app.portal_procurement.service import _dealer_id

    found: dict[int, Decimal] = {}
    for row in rows or []:
        dealer = _dealer_id(row)
        pct = _pct(row.get("discount_percent"))
        if dealer is not None and pct is not None:
            found[dealer] = pct
    _dealer_discounts[int(source_line_id)] = (now or now_ist_naive(), found)
    return found


def prefetch_dealer_discounts(demands: list[m.PortalDemandLine], client, limit: int = 60) -> int:
    """Read the Portal vendor options of these lines once. Never raises."""
    if not settings.use_known_discount or client is None:
        return 0
    from backend.app.portal_procurement.service import _dealer_rows

    now = now_ist_naive()
    ttl = timedelta(hours=settings.known_discount_ttl_hours)
    done = 0
    for demand in demands[:limit]:
        have = _dealer_discounts.get(int(demand.source_order_line_id))
        if have is not None and now - have[0] < ttl:
            continue
        try:
            remember_dealer_discounts(demand.source_order_line_id,
                                      _dealer_rows(client.vendors_for_line(demand.source_order_line_id)), now)
            done += 1
        except Exception:  # noqa: BLE001 -- not known -> the vendor is asked, as before
            logger.warning("Portal line %s: vendor discounts could not be read.", demand.source_order_line_id)
    return done


def dealer_discount(source_line_id: int, dealer_id: int | None, now: datetime | None = None) -> Decimal | None:
    have = _dealer_discounts.get(int(source_line_id))
    if have is None or dealer_id is None:
        return None
    if (now or now_ist_naive()) - have[0] >= timedelta(hours=settings.known_discount_ttl_hours):
        return None
    return have[1].get(dealer_id)


def known_discounts(lines: list[am.AdvanceOrderLine], vendor_id: int, brand: str, session: Session) -> dict[str, str]:
    """{line id: discount %} the vendor is NOT asked for."""
    if not settings.use_known_discount:
        return {}
    from backend.app.portal_procurement import links

    dealer = links.resolve(vendor_id, session)
    if dealer is None:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        demand = demand_for_line(line, session)
        if demand is not None:
            sol = demand.source_order_line_id
        else:
            # The first question goes out inside create_order, before the
            # Portal line is linked to the order: match by part number among
            # the lines whose vendor discounts were just read.
            key = _norm(line.part_number)
            sol = next((d.source_order_line_id for d in session.execute(
                select(m.PortalDemandLine).where(
                    m.PortalDemandLine.source_order_line_id.in_(list(_dealer_discounts) or [-1]),
                    m.PortalDemandLine.advance_order_id.is_(None),
                )
            ).scalars() if _norm(d.part_no) == key), None)
        if sol is None:
            continue
        pct = dealer_discount(sol, dealer)
        if pct is not None:
            out[str(line.id)] = str(pct)
    return out


# ====================================================================== low discount alert (19 Nov 2026)
def check_low_discount(order: am.AdvanceOrder, q: am.AdvanceVendorQuery, session: Session) -> int:
    """A vendor quoting LESS than the customer's discount -> Prateek sir is
    told at once (he sees it while the round is still open). Returns how many
    parts were reported."""
    if not settings.low_discount_alert:
        return 0
    from backend.app.portal_procurement.service import margin_percent

    rows = []
    for line in order.lines:
        if line.id not in (q.line_ids or []):
            continue
        quote = session.execute(
            select(am.AdvanceVendorQuote).where(
                am.AdvanceVendorQuote.advance_order_line_id == line.id,
                am.AdvanceVendorQuote.vendor_id == q.vendor_id,
            )
        ).scalar_one_or_none()
        demand = demand_for_line(line, session)
        if quote is None or demand is None or not quote.available:
            continue
        cust, got = demand.customer_discount_pct, quote.discount_pct
        if cust is None or got is None or got >= cust:
            continue
        mg = margin_percent(demand, quote)
        rows.append(f"• {line.part_number} x{line.qty}: vendor {_money(got)}% de raha hai, customer ko {_money(cust)}% "
                    "diya hai" + (f" (margin {_money(mg)}%)" if mg is not None else ""))
    if not rows:
        return 0
    _send(f"⚠️ Kam discount — {_vendor(q.vendor_id, session)} ({q.brand or '-'}):\n" + "\n".join(rows)
          + "\n\nCustomer ke discount se kam hai. Round ke end tak dusre vendor ke jawab bhi dekhe jayenge.")
    logger.info("Portal rounds: vendor %s quoted below the customer's discount on %d part(s).", q.vendor_id, len(rows))
    return len(rows)


# ====================================================================== 6 hours with no good answer (19 Nov 2026)
def _quote_lines(line: am.AdvanceOrderLine, session: Session) -> list[str]:
    out = []
    for quote in session.execute(
        select(am.AdvanceVendorQuote).where(am.AdvanceVendorQuote.advance_order_line_id == line.id)
    ).scalars():
        if not quote.available:
            out.append(f"{_vendor(quote.vendor_id, session)}: nahi hai")
        else:
            days = f"{quote.tat_days} din" if quote.tat_days is not None else "din -"
            disc = f"{_money(quote.discount_pct)}%" if quote.discount_pct is not None else "discount -"
            out.append(f"{_vendor(quote.vendor_id, session)}: {days}, {disc}")
    return out


def escalate_stale(session: Session, now: datetime | None = None) -> int:
    """A Portal part with no usable answer `escalate_hours` after the first
    ask goes to Prateek sir, who can name a NEW vendor (`AO-12 vendor <name>
    <mobile>`) or cancel it. Counted in vendor hours. Returns parts escalated."""
    from backend.app.advance_orders import quotes, service as engine
    from backend.app.portal_procurement.config import SOURCE

    now = now or now_ist_naive()
    count = 0
    for order in session.execute(
        select(am.AdvanceOrder).where(am.AdvanceOrder.status == am.ASKING, am.AdvanceOrder.source == SOURCE)
    ).scalars().all():
        if not engine._workable(order):
            continue
        queries = session.execute(
            select(am.AdvanceVendorQuery).where(am.AdvanceVendorQuery.advance_order_id == order.id)
        ).scalars().all()
        stale: list[am.AdvanceOrderLine] = []
        for line in order.lines:
            if line.status != am.LINE_ASKING:
                continue
            sent = [q.sent_at for q in queries if q.sent_at is not None and line.id in (q.line_ids or [])]
            if not sent:
                continue
            if engine._add_vendor_hours(min(sent), settings.escalate_hours * 60) > now:
                continue
            demand = demand_for_line(line, session)
            if any(why_not(demand, s.quote) is None for s in quotes.ranked_for_line(line, session)):
                continue  # a good answer is on the table: the round decides it
            stale.append(line)
        if not stale:
            continue
        rows = []
        for line in stale:
            line.status = am.LINE_ESCALATED
            line.note = f"no good answer in {settings.escalate_hours} hours -- asked Prateek sir"
            said = _quote_lines(line, session)
            rows.append(f"• {line.part_number} ({line.brand}) x{line.qty} — " + ("; ".join(said) if said else "kisi ne reply nahi kiya"))
        session.flush()
        ref = f"AO-{order.id}"
        _send(
            f"🔎 {settings.escalate_hours} ghante ho gaye, in parts ka theek jawab nahi aaya ({ref}):\n"
            + "\n".join(rows)
            + f"\n\nAb kya karna hai? Reply:\n"
              f"• {ref} vendor Sharma Auto 98XXXXXXXX  (naya vendor — brand ki list mein save; unka group hone par poochhunga)\n"
              f"• {ref} cancel"
        )
        logger.info("Portal order %s: %d part(s) with no good answer after %d hours -- Prateek sir told.",
                    order.id, len(stale), settings.escalate_hours)
        count += len(stale)
    return count


# ====================================================================== batch rule (User, 21 Nov 2026)
# A round's parts are ONE batch: every vendor of the brand gets them at once
# and has ONE window (`batch_reply_minutes`, clock time) with one reminder
# halfway. A part with a good answer gets its winner (fewer days first, then
# the Vendors-page order, then the bigger discount) as soon as every vendor
# asked has answered, or at the end of the window. At the end, whatever has
# no good answer goes to the purchase team in ONE brand-wise message, and the
# vendors are not asked about that batch again. A reply after the window is
# kept for the vendor record only.
_BATCH = "batch"


def batch_on() -> bool:
    return bool(settings.rounds and settings.batch)


def _batch(order: am.AdvanceOrder) -> dict:
    return dict((order.dealer_portal or {}).get(_BATCH) or {})


def batch_end(order: am.AdvanceOrder) -> datetime | None:
    raw = _batch(order).get("end")
    return datetime.fromisoformat(raw) if raw else None


def start_batch(order: am.AdvanceOrder, session: Session, now: datetime) -> None:
    """Fix the order's window once: from its first question (or now)."""
    info = _batch(order)
    if info.get("end"):
        return
    sent = [q.sent_at for q in session.execute(
        select(am.AdvanceVendorQuery).where(am.AdvanceVendorQuery.advance_order_id == order.id)
    ).scalars() if q.sent_at is not None]
    start = min(sent) if sent else now
    info["start"] = start.isoformat(timespec="seconds")
    info["end"] = (start + timedelta(minutes=settings.batch_reply_minutes)).isoformat(timespec="seconds")
    order.dealer_portal = {**(order.dealer_portal or {}), _BATCH: info}


def decide_batch_line(line: am.AdvanceOrderLine, still_open: list, now: datetime, session: Session) -> bool:
    """A good answer wins once nobody asked is still to answer, or the window
    is over. No good answer: wait -- the end of the window reports it."""
    from backend.app.advance_orders import quotes

    demand = demand_for_line(line, session)
    good = [s for s in quotes.ranked_for_line(line, session) if why_not(demand, s.quote) is None]
    end = batch_end(line.order)
    over = end is not None and now >= end
    if good and (not still_open or over):
        return quotes.apply_winner(line, session, allowed={s.quote.id for s in good})
    return False


_WHY = (
    (re.compile(r"^(\d+) days \(over (\d+)\)$"), r"\2 din se zyada"),
    (re.compile(r"^margin (.+)% \(under (.+)%\)$"), r"margin \1%, \2% se kam"),
    (re.compile(r"^no days given$"), "din nahi bataye"),
    (re.compile(r"^margin unknown.*$"), "MRP / discount nahi bataya"),
)


def _why_hi(reason: str) -> str:
    for pattern, repl in _WHY:
        if pattern.match(reason):
            return pattern.sub(repl, reason)
    return reason


def _arrange_reason(line: am.AdvanceOrderLine, asked: bool, session: Session) -> str:
    from backend.app.advance_orders import quotes

    if not asked:
        return "kisi vendor se poocha nahi ja saka (brand ka vendor / group nahi)"
    ranked = quotes.ranked_for_line(line, session)
    offers = [s for s in ranked if s.quote.available]
    if offers:
        demand = demand_for_line(line, session)
        bits = []
        for s in offers[:3]:
            q = s.quote
            days = f"{q.tat_days} din" if q.tat_days is not None else "din -"
            disc = f"{_money(q.discount_pct)}%" if q.discount_pct is not None else "discount -"
            why = why_not(demand, q)
            bits.append(f"{_vendor(q.vendor_id, session)}: {days}, {disc}" + (f" — {_why_hi(why)}" if why else ""))
        return "rule se bahar — " + "; ".join(bits)
    if ranked:
        return "sab vendors ne mana kiya"
    return "kisi vendor ne reply nahi kiya"


def _close_batch_questions(order: am.AdvanceOrder, session: Session, now: datetime) -> None:
    from backend.app.advance_orders import service as engine

    for q in session.execute(
        select(am.AdvanceVendorQuery).where(am.AdvanceVendorQuery.advance_order_id == order.id)
    ).scalars().all():
        if q.status == am.Q_SENT and q.step and q.step not in ("pcs", "done") and q.draft:
            engine._finish_steps(order, q, session, now)  # what he did say is kept
        elif q.status == am.Q_SENT:
            q.status = am.Q_TIMEOUT
        elif q.status == am.Q_QUEUED:
            q.status = am.Q_CLOSED
        q.draft = {**(q.draft or {}), "_batch_closed": now.isoformat(timespec="seconds")}
    session.flush()


def batch_report(session: Session, now: datetime | None = None) -> int:
    """End of a batch window: decide what can be decided, hand the rest to
    the purchase team (one brand-wise message per batch). Returns parts
    handed over."""
    from backend.app.advance_orders import service as engine
    from backend.app.portal_procurement.config import SOURCE

    now = now or now_ist_naive()
    due: list[am.AdvanceOrder] = []
    for order in session.execute(
        select(am.AdvanceOrder).where(am.AdvanceOrder.source == SOURCE).order_by(am.AdvanceOrder.id.desc()).limit(500)
    ).scalars():
        info = _batch(order)
        if info.get("end") and not info.get("reported") and datetime.fromisoformat(info["end"]) <= now:
            due.append(order)
    if not due:
        return 0
    groups: dict[str, list[am.AdvanceOrder]] = {}
    for order in due:
        groups.setdefault(_batch(order)["start"][:16], []).append(order)
    handed = 0
    for start, orders in sorted(groups.items()):
        filled: dict[str, list[str]] = {}
        arrange: dict[str, list[str]] = {}
        for order in orders:
            _close_batch_questions(order, session, now)
            if order.status == am.ASKING:
                engine._resolve_lines(order, session, now)
            asked_lines = set()
            for q in session.execute(
                select(am.AdvanceVendorQuery).where(am.AdvanceVendorQuery.advance_order_id == order.id,
                                                    am.AdvanceVendorQuery.status != am.Q_FAILED)
            ).scalars():
                asked_lines.update(q.line_ids or [])
            for line in order.lines:
                brand = line.brand or "-"
                if line.status in (am.LINE_AVAILABLE, am.LINE_ORDERED) and line.vendor_id:
                    days = f"{line.tat_days} din" if line.tat_days is not None else "din -"
                    disc = f"{_money(line.discount_pct)}%" if line.discount_pct is not None else "discount -"
                    filled.setdefault(brand, []).append(
                        f"{line.part_number} x{line.qty} — {_vendor(line.vendor_id, session)}, {days}, {disc}")
                elif line.status in (am.LINE_ASKING, am.LINE_UNAVAILABLE, am.LINE_ESCALATED):
                    # A split part's remainder was never asked on its own: its
                    # vendors are the original line's.
                    asked = line.id in asked_lines or any(
                        other.part_number == line.part_number and other.id in asked_lines for other in order.lines)
                    arrange.setdefault(brand, []).append(
                        f"{line.part_number} x{line.qty} — {_arrange_reason(line, asked, session)}")
                    # Not LINE_ESCALATED: that would make Prateek sir's next
                    # message look like an answer to an AO escalation.
                    line.status = am.LINE_UNAVAILABLE
                    line.note = "batch: no good answer in 3 hours -- purchase team told to arrange it"
                    handed += 1
            engine._settle(order)
            order.dealer_portal = {**(order.dealer_portal or {}),
                                   _BATCH: {**_batch(order), "reported": now.isoformat(timespec="seconds")}}
        session.flush()
        if arrange:
            _send_batch(batch_text(datetime.fromisoformat(start), filled, arrange))
            logger.info("Portal batch %s: %d part(s) to arrange -- purchase team told.", start,
                        sum(len(v) for v in arrange.values()))
    return handed


def batch_text(start: datetime, filled: dict[str, list[str]], arrange: dict[str, list[str]]) -> str:
    n_fill = sum(len(v) for v in filled.values())
    n_arr = sum(len(v) for v in arrange.values())
    end = start + timedelta(minutes=settings.batch_reply_minutes)
    out = [
        f"📦 *Purchase batch — {start:%d %b, %H:%M}* (reply time {end:%H:%M} tak, poora ho gaya)",
        f"Kul {n_fill + n_arr} part · ✅ {n_fill} mil gaye · ❗ {n_arr} arrange karne hain",
    ]
    for brand in sorted(set(filled) | set(arrange)):
        got, need = filled.get(brand, []), arrange.get(brand, [])
        out += ["", f"*{brand}* — {len(got) + len(need)} part"]
        if got:
            out.append("✅ Mil gaye (PO-MST ban raha hai):")
            out += [f"  {i}. {row}" for i, row in enumerate(got, start=1)]
        if need:
            out.append("❗ Arrange karna hai:")
            out += [f"  {i}. {row}" for i, row in enumerate(need, start=1)]
    out += ["", "In parts ke liye ab vendors se nahi poocha jayega. Arrange karke Dealer Portal par allocate kar dijiye."]
    return "\n".join(out)


def batch_numbers() -> list[str]:
    from backend.app.advance_orders.service import _normalize

    return [n for n in dict.fromkeys(_normalize(x) for x in settings.batch_escalation_numbers) if n]


def _send_batch(text: str) -> None:
    from backend.app.advance_orders.service import _send_text

    for number in batch_numbers():
        try:
            _send_text(number, text)
        except Exception:  # noqa: BLE001 -- the next number is tried
            logger.exception("Portal batch: message to %s failed.", number)


def note_late_reply(number: str, text: str, session: Session, now: datetime | None = None) -> bool:
    """A vendor answering after his batch closed: kept on that question for
    his record (`_late`), never used to pick a winner. True when kept."""
    now = now or now_ist_naive()
    for q in session.execute(
        select(am.AdvanceVendorQuery)
        .where(am.AdvanceVendorQuery.status.in_((am.Q_TIMEOUT, am.Q_CLOSED)))
        .order_by(am.AdvanceVendorQuery.id.desc())
        .limit(300)
    ).scalars():
        draft = q.draft or {}
        if number not in (q.numbers or []) or not draft.get("_batch_closed") or draft.get("_late"):
            continue
        if q.sent_at is None or q.sent_at < now - timedelta(hours=24):
            continue
        q.draft = {**draft, "_late": {"at": now.isoformat(timespec="seconds"), "text": (text or "")[:500]}}
        session.flush()
        logger.info("Portal batch: late reply from vendor %s kept for his record (question %s).", q.vendor_id, q.id)
        return True
    return False
