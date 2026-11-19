"""Portal procurement orchestrator.

    poll()        GET /procurement/active for the last 7 days (paginated) ->
                  new pending source lines are stored as `discovered`
    enquire()     discovered lines -> the advance-order engine asks every
                  vendor of the brand (one message per vendor per brand,
                  at most `max_lines_per_enquiry` parts, at most
                  `max_new_lines_per_poll` new lines per poll)
    collect()     lines the engine decided (ranked: TAT band, then discount)
                  -> one `PortalAllocation` per winning quote / split
    run_steps()   every allocation and PO-MST moves one step at a time:
                  vendor identity -> re-fetch quantity -> allocate -> knockoff
                  preview -> safe commit or Super Admin approval -> Odoo ->
                  PDF -> WhatsApp to the purchase user

Every state change is a compare-and-set UPDATE (`_move`) with an audit row.
Every POST is preceded by a committed "submitting/posting" state, so a crash
or lost response is reconciled by GET and never re-POSTed blind."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as am
from backend.app.portal_procurement import models as m
from backend.app.portal_procurement.client import AuthStopped, PortalClient, PortalError, UnknownOutcome
from backend.app.portal_procurement.config import SOURCE, settings
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

# Allocation states the step runner works on, and PO-MST states likewise.
_ALLOC_ACTIVE = (m.WINNER_SELECTED, m.VENDOR_ONBOARDING_REQUIRED, m.VENDOR_MAPPED,
                 m.ALLOCATION_SUBMITTING, m.ALLOCATION_UNKNOWN)
_PO_ACTIVE = (m.PO_MST_CREATED, m.KNOCKOFF_PREVIEWED, m.SAFE_KNOCKOFF_COMMITTED, m.APPROVAL_PENDING,
              m.APPROVAL_APPROVED, m.APPROVAL_REJECTED, m.ODOO_POSTING, m.ODOO_POSTED, m.PDF_DOWNLOADED,
              m.PURCHASE_USER_NOTIFIED)
_DEMAND_OPEN = (m.DISCOVERED, m.ENQUIRY_SCHEDULED, m.AWAITING_VENDOR_REPLIES, m.MARGIN_HOLD)


# ====================================================================== helpers
def _norm_part(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _dec(value: Any) -> Decimal | None:
    try:
        if value is None or value == "":
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _first(row: dict, *keys: str) -> Any:
    for key in keys:
        if isinstance(row, dict) and row.get(key) not in (None, ""):
            return row[key]
    return None


def _rows(body: Any) -> list[dict]:
    """The list of rows in a Portal list response, whatever it is wrapped in."""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        for key in ("items", "rows", "results", "records", "data", "vendors", "lines"):
            value = body.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                inner = _rows(value)
                if inner:
                    return inner
    return []


def _total(body: Any) -> int | None:
    if isinstance(body, dict):
        for key in ("total", "count", "total_count"):
            if _int(body.get(key)) is not None:
                return _int(body.get(key))
        if isinstance(body.get("data"), dict):
            return _total(body["data"])
        if isinstance(body.get("pagination"), dict):
            return _total(body["pagination"])
    return None


def _last_page(rows: list, total: int | None, offset: int) -> bool:
    if not rows:
        return True
    if total is not None:
        return offset >= total
    return len(rows) < settings.page_size


def _money(value: Decimal | None, places: str = "0.01") -> float | None:
    if value is None:
        return None
    return float(Decimal(value).quantize(Decimal(places)))


def _err(exc: PortalError) -> str:
    text = str(exc)
    if exc.detail:
        text = f"{text}: {exc.detail}"
    return text[:500]


def _move(session: Session, obj, to: str, *, entity: str, actor: str = "bot", error: str | None = None,
          http_status: int | None = None, request_id: str | None = None, **values) -> bool:
    """Compare-and-set state change + audit row. False when another worker
    already moved the row (it is then left alone)."""
    model = type(obj)
    frm = obj.state
    if frm == to:
        return False
    result = session.execute(
        update(model)
        .where(model.id == obj.id, model.state == frm)
        .values(state=to, updated_at=now_ist_naive(), **values)
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:
        session.refresh(obj)
        return False
    session.refresh(obj)
    demand_id = None
    quote_ref = None
    po = None
    if entity == "demand":
        demand_id = obj.source_order_line_id
    elif entity == "allocation":
        quote_ref = obj.quote_reference
        demand = session.get(m.PortalDemandLine, obj.demand_line_id)
        demand_id = demand.source_order_line_id if demand else None
    elif entity == "po_mst":
        po = obj.po_mst
    session.add(m.PortalTransition(
        entity=entity, entity_id=obj.id, source_order_line_id=demand_id, quote_reference=quote_ref,
        po_mst=po, from_state=frm, to_state=to, actor=actor, http_status=http_status,
        request_id=request_id, error=(error or None) and error[:500],
    ))
    session.flush()
    logger.info("Portal procurement: %s %s %s -> %s%s", entity, obj.id, frm, to, f" ({error})" if error else "")
    return True


# ------------------------------------------------------------------ messaging
_alerted: dict[str, datetime] = {}


def _numbers(which: list[str], session: Session) -> list[str]:
    if which:
        return which
    from backend.app.integrations.whatsapp.recipients import internal_file_recipients

    return internal_file_recipients(session)


def _alert(session: Session, key: str, text: str, hours: int = 6) -> None:
    """Tell operations, at most once per `hours` for the same key. Never
    carries a token, password or Authorization header."""
    now = now_ist_naive()
    last = _alerted.get(key)
    if last and now - last < timedelta(hours=hours):
        return
    _alerted[key] = now
    from backend.app.advance_orders.service import _send_text

    for number in _numbers(settings.alert_numbers, session):
        try:
            _send_text(number, f"⚠️ Portal procurement: {text}")
        except Exception:  # noqa: BLE001 -- an alert must never break the run
            logger.exception("Portal procurement alert to %s failed.", number)


# ====================================================================== poll
def _window(today: date) -> tuple[str, str]:
    return (today - timedelta(days=settings.days - 1)).isoformat(), today.isoformat()


def _row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:32]


# Portal team, 13 Nov 2026: a source line already po_confirmed with an earlier
# vendor is still open for (re)assignment through a new PO-MST.
WORKABLE_STAGES = ("pending", "po_confirmed")
_DEAD_STATUSES = ("rejected", "closed", "cancelled", "canceled", "received")


def _eligible(row: dict) -> bool:
    """row_type == source AND planned_quantity > 0 AND editable == true AND
    procurement_stage IN (pending, po_confirmed) -- and not closed/cancelled."""
    if str(_first(row, "row_type") or "").lower() != "source":
        return False
    if (_int(_first(row, "planned_quantity")) or 0) <= 0:
        return False
    if row.get("editable") is not True:
        return False
    if str(_first(row, "procurement_stage") or "").lower() not in WORKABLE_STAGES:
        return False
    if row.get("is_closed") is True:
        return False
    if str(_first(row, "status", "order_status") or "").lower() in _DEAD_STATUSES:
        return False
    return _int(_first(row, "source_order_line_id")) is not None


def _master_for(rows: list[dict], sol: int) -> dict | None:
    """The live PO-MST allocation of a source line: a master row for the same
    source_order_line_id that carries an allocation_id and is editable. Its
    allocation_id makes the allocate call SUPERSEDE it instead of adding one."""
    for row in rows:
        if (str(row.get("row_type") or "").lower() == "master"
                and _int(row.get("source_order_line_id")) == sol
                and _int(row.get("allocation_id")) is not None
                and row.get("editable") is True):
            return row
    return None


def _buyer(row: dict) -> dict:
    keys = ("buyer_dealer_id", "buyer_dealer_name", "dealer_id", "dealer_name", "branch", "branch_name",
            "buyer_branch", "company", "customer_so", "order_id")
    return {k: row[k] for k in keys if k in row and row[k] not in (None, "")}


def _customer_side(row: dict) -> dict:
    """MRP, the customer's discount % and the price he pays, from a Portal row."""
    return {
        "portal_mrp": _dec(_first(row, "mrp")),
        "customer_discount_pct": _dec(_first(row, "customer_discount_percent")),
        "sale_price": _dec(_first(row, "price")),
    }


def poll(session: Session, client: PortalClient, today: date | None = None) -> dict:
    """Read the last-7-days demand, every page. Returns counts."""
    today = today or now_ist_naive().date()
    date_from, date_to = _window(today)
    offset = 0
    seen: set[int] = set()
    new = updated = pages = 0
    received: dict[int, int] = {}
    complete = False
    while True:
        body = client.active(date_from, date_to, offset, settings.page_size)
        rows = _rows(body)
        total = _total(body)
        pages += 1
        for row in rows:
            got = _int(_first(row, "qty_received")) or 0
            sol_any = _int(_first(row, "source_order_line_id"))
            if got > 0 and sol_any is not None:
                received[sol_any] = max(received.get(sol_any, 0), got)
            if not _eligible(row):
                continue
            sol = _int(row["source_order_line_id"])
            seen.add(sol)
            qty = _int(row.get("planned_quantity")) or 0
            existing = session.execute(
                select(m.PortalDemandLine).where(m.PortalDemandLine.source_order_line_id == sol)
            ).scalar_one_or_none()
            digest = _row_hash(row)
            if existing is None:
                session.add(m.PortalDemandLine(
                    source_order_line_id=sol,
                    source_po_group_id=_first(row, "source_po_group_id", "po_group_id"),
                    part_no=str(_first(row, "part_no", "part_number") or "").strip().upper(),
                    brand=(str(_first(row, "brand")).strip().upper() if _first(row, "brand") else None),
                    planned_quantity=qty,
                    order_date=_date(_first(row, "order_date")),
                    buyer=_buyer(row),
                    procurement_stage=str(_first(row, "procurement_stage") or ""),
                    row_hash=digest,
                    last_seen_at=now_ist_naive(),
                    **_customer_side(row),
                ))
                new += 1
            else:
                existing.last_seen_at = now_ist_naive()
                for key, value in _customer_side(row).items():
                    setattr(existing, key, value)
                if existing.row_hash != digest:
                    existing.row_hash = digest
                    existing.planned_quantity = qty
                    existing.procurement_stage = str(_first(row, "procurement_stage") or "")
                    updated += 1
        offset += len(rows)
        if _last_page(rows, total, offset) or pages >= 200:
            complete = pages < 200 or _last_page(rows, total, offset)
            break
    session.flush()
    try:
        from backend.app.portal_procurement import rounds

        rounds.note_receipts(session, received)
    except Exception:  # noqa: BLE001 -- the delivery record never blocks the poll
        logger.exception("Portal procurement: could not record deliveries.")
    closed = 0
    if complete:
        # A line still being enquired that is no longer pending on the Portal
        # (a person allocated it, or it was cancelled): stop asking about it.
        start = date.fromisoformat(date_from)
        for demand in session.execute(
            select(m.PortalDemandLine).where(m.PortalDemandLine.state.in_(_DEMAND_OPEN))
        ).scalars().all():
            if demand.source_order_line_id in seen:
                continue
            if demand.order_date is not None and demand.order_date < start:
                continue  # aged out of the window, not closed -- keep going
            _close_demand(session, demand, "no longer pending on the Portal")
            closed += 1
    logger.info("Portal procurement poll %s..%s: %d page(s), %d new, %d changed, %d closed.",
                date_from, date_to, pages, new, updated, closed)
    return {"new": new, "updated": updated, "closed": closed, "pages": pages}


def _order_lines_for(demand: m.PortalDemandLine, session: Session) -> list[am.AdvanceOrderLine]:
    if not demand.advance_order_id:
        return []
    order = session.get(am.AdvanceOrder, demand.advance_order_id)
    if order is None:
        return []
    key = _norm_part(demand.part_no)
    return [line for line in order.lines if _norm_part(line.part_number) == key]


def _close_demand(session: Session, demand: m.PortalDemandLine, why: str) -> None:
    for line in _order_lines_for(demand, session):
        if line.status in (am.LINE_ASKING, am.LINE_ESCALATED, am.LINE_AVAILABLE):
            line.status = am.LINE_CANCELLED
            line.note = f"Portal: {why}"
    order = session.get(am.AdvanceOrder, demand.advance_order_id) if demand.advance_order_id else None
    if order is not None and order.status == am.ASKING and all(
        line.status not in (am.LINE_ASKING, am.LINE_ESCALATED) for line in order.lines
    ):
        from backend.app.advance_orders.service import _settle

        _settle(order)
    _move(session, demand, m.CLOSED, entity="demand", error=why, note=why)


# ====================================================================== enquire
def _external_ref(demands: list[m.PortalDemandLine]) -> str:
    # updated_at is in the key so a line reopened after a rejection gets a
    # NEW enquiry (create_order returns the existing order for a known ref).
    ids = ",".join(
        f"{d.source_order_line_id}@{d.updated_at.isoformat() if d.updated_at else ''}"
        for d in sorted(demands, key=lambda d: d.source_order_line_id)
    )
    return "PORTAL-" + hashlib.sha1(ids.encode()).hexdigest()[:12].upper()


def enquire(session: Session, client: PortalClient | None = None) -> int:
    """Discovered lines -> vendor questions through the advance-order engine.
    One order per brand chunk; a part appears once per order so a vendor's
    answer maps back to exactly one Portal line. With a Portal `client`, each
    line's vendor discounts are read first so a vendor whose discount is
    already set is not asked for it (19 Nov 2026)."""
    from backend.app.advance_orders import service as engine

    query = select(m.PortalDemandLine).where(m.PortalDemandLine.state == m.DISCOVERED)
    pilot = None if settings.live() else settings.pilot()
    if pilot is not None and not (pilot["lines"] or pilot["parts"] or pilot["brands"]):
        return 0  # pilot with nothing chosen: nobody is asked
    fresh = session.execute(
        query.order_by(m.PortalDemandLine.order_date.asc().nulls_last(), m.PortalDemandLine.id)
    ).scalars().all()
    if pilot is not None:
        # Pilot: only the chosen source lines / part numbers.
        from backend.app.advance_orders.brands import canonical_brand

        fresh = [d for d in fresh if d.source_order_line_id not in pilot["skip_lines"] and (
                 d.source_order_line_id in pilot["lines"]
                 or _norm_part(d.part_no) in pilot["parts"]
                 or canonical_brand(d.brand) in pilot["brands"])]
    fresh = fresh[: settings.max_new_lines_per_poll]
    if not fresh:
        return 0
    if client is not None:
        from backend.app.portal_procurement import rounds

        rounds.prefetch_dealer_discounts(fresh, client)
    by_brand: dict[str, list[m.PortalDemandLine]] = {}
    for demand in fresh:
        if not demand.part_no:
            _move(session, demand, m.CLOSED, entity="demand", error="no part number on the Portal row")
            continue
        by_brand.setdefault(demand.brand or "*", []).append(demand)
    asked = 0
    for brand, demands in by_brand.items():
        chunks: list[list[m.PortalDemandLine]] = [[]]
        for demand in demands:
            chunk = chunks[-1]
            if len(chunk) >= settings.max_lines_per_enquiry or any(
                _norm_part(d.part_no) == _norm_part(demand.part_no) for d in chunk
            ):
                chunk = []
                chunks.append(chunk)
            chunk.append(demand)
        for chunk in chunks:
            if not chunk:
                continue
            payload = {
                "external_ref": _external_ref(chunk),
                "source": SOURCE,
                "requested_by": "Dealer Portal Active Procurement",
                "lines": [
                    {"part_number": d.part_no, "qty": d.planned_quantity,
                     "brand": None if brand == "*" else brand}
                    for d in chunk
                ],
            }
            try:
                order = engine.create_order(payload, session)
            except ValueError as exc:
                for d in chunk:
                    _move(session, d, m.CLOSED, entity="demand", error=f"could not enquire: {exc}")
                continue
            for d in chunk:
                if _move(session, d, m.ENQUIRY_SCHEDULED, entity="demand", advance_order_id=order.id):
                    asked += 1
    session.flush()
    logger.info("Portal procurement: %d line(s) sent to vendor enquiry.", asked)
    return asked


# ====================================================================== collect
def _quote_fields(line: am.AdvanceOrderLine, session: Session) -> dict:
    """What the winning quote says, in the Portal's terms.

    User, 11 Nov 2026: vendor prices are MRP based -- the MRP INCLUDES GST and
    the vendor gives a discount on that MRP. So every price the bot holds is
    GST-inclusive:
        net (incl. GST) = quoted rate, else MRP x (1 - discount %)
        rate ex-GST     = net / (1 + GST %)        (GST 18% by default)
    PORTAL_PROCUREMENT_VENDOR_RATE_INCLUDES_GST=false would treat a quoted
    rate as already ex-GST; PORTAL_PROCUREMENT_GST_PERCENT changes the 18%.
    No price known -> no rate is sent (MRP / discount / TAT still are)."""
    import os

    quote = session.get(am.AdvanceVendorQuote, line.winning_quote_id) if line.winning_quote_id else None
    gst = _dec(os.environ.get("PORTAL_PROCUREMENT_GST_PERCENT") or "18") or Decimal("18")
    rate_incl = (os.environ.get("PORTAL_PROCUREMENT_VENDOR_RATE_INCLUDES_GST") or "true").lower() == "true"
    divisor = 1 + gst / 100
    rate_ex = None
    if quote is not None and quote.quoted_rate is not None:
        rate_ex = quote.quoted_rate / divisor if rate_incl else quote.quoted_rate
    elif line.net_price is not None:
        rate_ex = line.net_price / divisor
    elif line.mrp is not None and line.discount_pct is not None:
        rate_ex = line.mrp * (1 - line.discount_pct / 100) / divisor
    return {
        "mrp": line.mrp,
        "rate_ex": rate_ex,
        "discount": line.discount_pct,
        "tat": line.tat_days,
    }


# ====================================================================== minimum margin
def margin_percent(demand: m.PortalDemandLine, quote) -> Decimal | None:
    """What we keep on this part with this vendor, in % of MRP:
    (what the customer pays - what the vendor charges) / MRP. Both incl. GST.
    None when either side is not known."""
    mrp = demand.portal_mrp if demand.portal_mrp and demand.portal_mrp > 0 else (
        quote.mrp if quote.mrp and quote.mrp > 0 else None)
    cust = demand.customer_discount_pct
    vend = quote.discount_pct
    if mrp is None:
        return (vend - cust) if vend is not None and cust is not None else None
    sale = demand.sale_price if demand.sale_price and demand.sale_price > 0 else (
        mrp * (1 - cust / 100) if cust is not None else None)
    cost = quote.net_price if quote.net_price is not None else (mrp * (1 - vend / 100) if vend is not None else None)
    if sale is None or cost is None:
        return None
    return ((sale - cost) / mrp * 100).quantize(Decimal("0.01"))


def _customer_known(demand: m.PortalDemandLine) -> bool:
    return demand.customer_discount_pct is not None or (demand.sale_price is not None and demand.sale_price > 0)


def _point_line_at(line: am.AdvanceOrderLine, quote, note: str) -> None:
    line.vendor_id = quote.vendor_id
    line.available_qty = min(quote.available_qty or line.qty, line.qty)
    line.eta_date = quote.eta_date
    line.tat_days = quote.tat_days
    line.mrp = quote.mrp
    line.discount_pct = quote.discount_pct
    line.net_price = quote.net_price
    line.winning_quote_id = quote.id
    line.note = note[:500]


def _margin_check(session: Session, demand: m.PortalDemandLine, line: am.AdvanceOrderLine) -> bool:
    """True = go ahead with the line's (possibly changed) winner. Among the
    vendors who keep the minimum margin, the engine's order stands (fastest
    first, then the bigger discount). None keeps it -> held, team told."""
    floor = Decimal(str(settings.min_margin_percent or 0))
    if floor <= 0:
        return True
    if (demand.choice_decision or "").startswith("quote:"):
        return True  # Prateek sir chose this vendor himself
    if not _customer_known(demand):
        logger.info("Portal line %s: customer price not known -- margin not checked.", demand.source_order_line_id)
        return True
    from backend.app.advance_orders import quotes

    ranked = quotes.ranked_for_line(line, session)
    checked = [(s.quote, margin_percent(demand, s.quote)) for s in ranked]
    ok = [(q, mg) for q, mg in checked if mg is not None and mg >= floor]
    if ok:
        quote, mg = ok[0]
        if quote.id != line.winning_quote_id:
            old = _vendor_name(line.vendor_id, session)
            _point_line_at(line, quote, f"{line.note or ''} - margin rule: {old} below {floor}%, "
                                        f"{_vendor_name(quote.vendor_id, session)} keeps {mg}%")
            logger.info("Portal line %s: winner changed for the minimum margin (%s%%).",
                        demand.source_order_line_id, mg)
        else:
            line.note = f"{line.note or ''} - margin {mg}%"[:500]
        return True
    # Nobody keeps the margin: hold, tell the team the best on the table.
    best_q, best_m = max(checked, key=lambda x: x[1] if x[1] is not None else Decimal("-1000"), default=(None, None))
    cust = f"{demand.customer_discount_pct:g}%" if demand.customer_discount_pct is not None else "-"
    sale = f"Rs {demand.sale_price:.2f}" if demand.sale_price is not None else "-"
    if best_q is not None:
        vend = f"{best_q.discount_pct:g}%" if best_q.discount_pct is not None else "discount not given"
        best = (f"best: {_vendor_name(best_q.vendor_id, session)} {vend}, "
                f"margin {best_m if best_m is not None else 'unknown'}%")
    else:
        best = "no usable quote"
    why = (f"margin below {floor}%: customer {cust} off (pays {sale}), {best}")
    _move(session, demand, m.MARGIN_HOLD, entity="demand", error=why, note=why)
    _alert(session, f"margin-{demand.source_order_line_id}",
           f"{demand.part_no} x{demand.planned_quantity} ({demand.brand or '-'}) held -- {why}. "
           "No PO-MST made. Decide on the Portal.", hours=24)
    return False


def collect(session: Session) -> int:
    """Turn every line the engine decided into an allocation; mark lines no
    vendor could supply."""
    made = 0
    for demand in session.execute(
        select(m.PortalDemandLine).where(
            m.PortalDemandLine.state.in_((m.ENQUIRY_SCHEDULED, m.AWAITING_VENDOR_REPLIES, m.QUOTES_NORMALIZED,
                                          m.WINNER_SELECTED))
        )
    ).scalars().all():
        lines = _order_lines_for(demand, session)
        if not lines:
            continue
        if demand.state == m.ENQUIRY_SCHEDULED:
            sent = session.execute(
                select(am.AdvanceVendorQuery.id).where(
                    am.AdvanceVendorQuery.advance_order_id == demand.advance_order_id,
                    am.AdvanceVendorQuery.status.in_((am.Q_SENT, am.Q_REPLIED, am.Q_TIMEOUT)),
                ).limit(1)
            ).first()
            if sent:
                _move(session, demand, m.AWAITING_VENDOR_REPLIES, entity="demand")
        for line in lines:
            if line.status != am.LINE_AVAILABLE or not line.vendor_id:
                continue
            exists = session.execute(
                select(m.PortalAllocation.id).where(m.PortalAllocation.advance_order_line_id == line.id)
            ).first()
            if exists:
                continue
            if demand.state in (m.ENQUIRY_SCHEDULED, m.AWAITING_VENDOR_REPLIES):
                _move(session, demand, m.QUOTES_NORMALIZED, entity="demand")
            if not _margin_check(session, demand, line):
                break  # held: no PO-MST for this line; the team decides
            fields = _quote_fields(line, session)
            today = now_ist_naive().strftime("%Y%m%d")
            session.add(m.PortalAllocation(
                demand_line_id=demand.id,
                advance_order_line_id=line.id,
                quote_id=line.winning_quote_id,
                vendor_id=line.vendor_id,
                quantity=line.available_qty or line.qty,
                quoted_mrp_incl_gst=fields["mrp"],
                quoted_rate_ex_gst=fields["rate_ex"],
                quoted_discount_percent=fields["discount"],
                quoted_tat_days=fields["tat"],
                quote_reference=f"PH-Q-{today}-{line.id:06d}",
                idempotency_key=f"alloc-{demand.source_order_line_id}-{line.id}",
                ranking_reason=(line.note or "")[:500],
                state=m.WINNER_SELECTED,
            ))
            # The line is ours now: the engine must not confirm it to the vendor.
            line.status = am.LINE_ORDERED
            session.flush()
            made += 1
            if demand.state == m.QUOTES_NORMALIZED:
                _move(session, demand, m.WINNER_SELECTED, entity="demand")
        if all(line.status in (am.LINE_UNAVAILABLE, am.LINE_CANCELLED) for line in lines) and demand.state in (
            m.ENQUIRY_SCHEDULED, m.AWAITING_VENDOR_REPLIES
        ):
            why = "; ".join(sorted({line.note or "no vendor had it" for line in lines}))[:300]
            _move(session, demand, m.NO_VENDOR, entity="demand", error=why, note=why)
    session.flush()
    return made


# ====================================================================== steps: allocation
def _dealer_rows(body: Any) -> list[dict]:
    return _rows(body)


def _dealer_id(row: dict) -> int | None:
    return _int(_first(row, "dealer_id", "supplier_dealer_id", "vendor_dealer_id", "id"))


def _resolve_dealer(alloc: m.PortalAllocation, demand: m.PortalDemandLine, client: PortalClient,
                    session: Session) -> tuple[int | None, str]:
    """(dealer id, why not). The winner's Portal dealer id only when it is
    LINKED by immutable id (`links.resolve`: the admin-confirmed stock map
    first, then the procurement links) AND the Portal's own vendor options
    for this line contain it -- never by name."""
    from backend.app.portal_procurement import links

    options = _dealer_rows(client.vendors_for_line(demand.source_order_line_id))
    links.refresh_dealers(session, options, demand.brand)
    by_id = {_dealer_id(r): r for r in options if _dealer_id(r) is not None}
    linked = links.resolve(alloc.vendor_id, session)
    if linked is None:
        return None, "the vendor is not linked to a Portal dealer yet"
    if linked not in by_id:
        return None, (f"linked to Portal dealer {linked}, but the Portal does not offer that dealer for this "
                      f"part (brand {demand.brand or '?'} mapping or Odoo partner missing on the Portal)")
    return linked, ""


def _vendor_gstins(vendor_id: int | None, session: Session) -> set[str]:
    if not vendor_id:
        return set()
    try:
        from backend.app.vendor_onboarding.models import VendorRegistrationRequest

        rows = session.execute(
            select(VendorRegistrationRequest.gstin).where(
                VendorRegistrationRequest.vendor_id == vendor_id, VendorRegistrationRequest.gstin.is_not(None)
            )
        ).scalars().all()
        return {str(g).strip().upper() for g in rows if g}
    except Exception:  # noqa: BLE001
        return set()


def _current_row(demand: m.PortalDemandLine, client: PortalClient, today: date) -> tuple[dict | None, dict | None]:
    """Re-fetch this demand line right before allocating (brief: never from
    stale quantity). Returns (source row, live PO-MST master row or None).
    Matched on source_order_line_id only."""
    date_from, date_to = _window(today)
    if demand.order_date is not None and demand.order_date.isoformat() < date_from:
        date_from = demand.order_date.isoformat()
    offset = 0
    seen: list[dict] = []
    while True:
        body = client.active(date_from, date_to, offset, settings.page_size)
        rows = _rows(body)
        seen += [r for r in rows if _int(r.get("source_order_line_id")) == demand.source_order_line_id]
        offset += len(rows)
        if _last_page(rows, _total(body), offset):
            break
    source = next((r for r in seen if str(r.get("row_type") or "").lower() == "source"), None)
    return source, _master_for(seen, demand.source_order_line_id)


def _not_allocatable(row: dict | None, demand: m.PortalDemandLine, qty: int) -> str | None:
    """Why this re-fetched source row must NOT be allocated now (None = ok)."""
    if row is None:
        return "line no longer on Active Procurement"
    planned = _int(row.get("planned_quantity")) or 0
    stage = str(row.get("procurement_stage") or "").lower()
    if planned <= 0:
        return f"planned quantity is {planned}"
    if not _eligible(row):
        return f"not open for assignment (stage {stage or '-'}, editable {row.get('editable')})"
    if _norm_part(row.get("part_no")) != _norm_part(demand.part_no):
        return f"part changed on the Portal ({demand.part_no} -> {row.get('part_no')})"
    if (_int(row.get("qty_received")) or 0) > 0:
        return f"already received ({row.get('qty_received')})"
    return None


def _vendor_name(vendor_id: int | None, session: Session) -> str:
    from core.models import Vendor

    vendor = session.get(Vendor, vendor_id) if vendor_id else None
    return vendor.name if vendor else f"vendor {vendor_id}"


def _allocation_payload(alloc: m.PortalAllocation, demand: m.PortalDemandLine, qty: int,
                        allocation_id: int | None = None) -> dict:
    payload: dict[str, Any] = {
        "source_order_line_id": demand.source_order_line_id,
        "supplier_dealer_id": alloc.supplier_dealer_id,
        "planned_quantity": qty,
        "quote_reference": alloc.quote_reference,
        "quote_source": "vendor_bot",
    }
    if allocation_id is not None:
        # An existing PO-MST allocation is superseded atomically by this one.
        payload["allocation_id"] = int(allocation_id)
    if alloc.quoted_mrp_incl_gst is not None and alloc.quoted_mrp_incl_gst > 0:
        payload["quoted_mrp_incl_gst"] = _money(alloc.quoted_mrp_incl_gst)
    if alloc.quoted_rate_ex_gst is not None and alloc.quoted_rate_ex_gst > 0:
        payload["quoted_rate_ex_gst"] = _money(alloc.quoted_rate_ex_gst)
    if alloc.quoted_discount_percent is not None and 0 <= alloc.quoted_discount_percent <= 100:
        payload["quoted_discount_percent"] = _money(alloc.quoted_discount_percent)
    if alloc.quoted_tat_days is not None and 0 <= alloc.quoted_tat_days <= 365:
        payload["quoted_tat_days"] = int(alloc.quoted_tat_days)
    return payload


def _masters(body: Any) -> list[dict]:
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    masters = data.get("masters") if isinstance(data, dict) else None
    return [x for x in masters if isinstance(x, dict) and x.get("po_mst")] if isinstance(masters, list) else []


def _record_masters(session: Session, alloc: m.PortalAllocation, masters: list[dict], batch_id: Any) -> None:
    if alloc.promised_date is None:
        from backend.app.portal_procurement import rounds

        arrival = next((str(x.get("arrival_date")) for x in masters if x.get("arrival_date")), None)
        alloc.promised_date = rounds.promise_date(alloc, arrival)
    for item in masters:
        po = str(item["po_mst"])
        row = session.execute(select(m.PortalPoMaster).where(m.PortalPoMaster.po_mst == po)).scalar_one_or_none()
        if row is None:
            session.add(m.PortalPoMaster(
                po_mst=po, allocation_ids=[alloc.id],
                vendor_dealer_id=_int(item.get("vendor_id")) or alloc.supplier_dealer_id,
                vendor_name=item.get("vendor"), brand=item.get("brand"),
                quantity=_int(item.get("quantity")), arrival_date=str(item.get("arrival_date") or "") or None,
            ))
        else:
            ids = list(row.allocation_ids or [])
            if alloc.id not in ids:
                ids.append(alloc.id)
                row.allocation_ids = ids
            if row.state not in (m.PO_MST_CREATED, m.KNOCKOFF_PREVIEWED):
                _alert(session, f"po-join-{po}",
                       f"allocation {alloc.quote_reference} was added to {po}, which is already at {row.state}. "
                       "Check it on the Portal.")
    session.flush()


def _tell_po_mst_created(session: Session, alloc: m.PortalAllocation, demand: m.PortalDemandLine,
                         masters: list[dict]) -> None:
    """User, 11 Nov 2026: every PO-MST the bot creates is reported to the
    purchase user (Shubham Maurya, PORTAL_PROCUREMENT_NOTIFY_NUMBERS) the
    moment it exists -- never to the vendor. Once per allocation (its move to
    po_mst_created is a compare-and-set, so a re-run cannot repeat it)."""
    from backend.app.advance_orders.service import _send_text

    rate = f"₹{alloc.quoted_rate_ex_gst:.2f} ex-GST" if alloc.quoted_rate_ex_gst is not None else "rate -"
    mrp = f"MRP ₹{alloc.quoted_mrp_incl_gst:.2f}" if alloc.quoted_mrp_incl_gst is not None else "MRP -"
    disc = f"{alloc.quoted_discount_percent:g}% off" if alloc.quoted_discount_percent is not None else "discount -"
    tat = f"{alloc.quoted_tat_days} din" if alloc.quoted_tat_days is not None else "TAT -"
    for item in masters:
        body = (
            "🧾 PO-MST created on the Dealer Portal\n"
            f"PO-MST: {item.get('po_mst')}\n"
            # No source PO-GRP line (User, 16 Nov 2026).
            f"Vendor: {item.get('vendor') or _vendor_name(alloc.vendor_id, session)}\n"
            f"Part: {demand.part_no} x{item.get('quantity') or alloc.quantity}"
            + (f" ({demand.brand})" if demand.brand else "") + "\n"
            + f"{mrp}, {disc}, {rate}, {tat}\n"
            + (f"Arrival: {item.get('arrival_date')}\n" if item.get("arrival_date") else "")
            + f"Quote: {alloc.quote_reference}\n\n"
            + "Knockoff check, Odoo PO and the PO PDF follow."
        )
        for number in _numbers(settings.notify_numbers, session):
            try:
                _send_text(number, body)
            except Exception:  # noqa: BLE001 -- never blocks the next step
                logger.exception("Portal procurement: PO-MST message to %s failed.", number)


def _bump(session: Session, obj, exc: PortalError | None, note: str | None = None, minutes: int = 10) -> None:
    obj.attempts = (obj.attempts or 0) + 1
    obj.error = (note or (_err(exc) if exc else None))
    obj.error_status = exc.status if exc else None
    obj.request_id = exc.request_id if exc else None
    obj.next_try_at = now_ist_naive() + timedelta(minutes=minutes * obj.attempts)
    session.flush()


def _due(obj) -> bool:
    return obj.next_try_at is None or obj.next_try_at <= now_ist_naive()


def _dead(session: Session, obj, entity: str, label: str) -> bool:
    if (obj.attempts or 0) < settings.max_step_attempts:
        return False
    _move(session, obj, m.DEAD_LETTER, entity=entity, error=obj.error)
    _alert(session, f"dead-{entity}-{obj.id}", f"{label} stopped after {obj.attempts} tries: {obj.error}", hours=24)
    return True


def _step_allocation(session: Session, alloc: m.PortalAllocation, client: PortalClient, today: date) -> None:
    demand = session.get(m.PortalDemandLine, alloc.demand_line_id)
    if demand is None:
        return
    label = f"{alloc.quote_reference} ({demand.part_no} x{alloc.quantity}, {_vendor_name(alloc.vendor_id, session)})"

    if alloc.state in (m.WINNER_SELECTED, m.VENDOR_ONBOARDING_REQUIRED):
        try:
            dealer, why = _resolve_dealer(alloc, demand, client, session)
        except PortalError as exc:
            _bump(session, alloc, exc)
            _dead(session, alloc, "allocation", label)
            return
        if dealer is None:
            # onboarding_needed: the quote is SAVED on the allocation and the
            # PO-MST is created by itself as soon as the link exists
            # (checked hourly, and at once after a link import).
            if alloc.state != m.VENDOR_ONBOARDING_REQUIRED:
                _move(session, alloc, m.VENDOR_ONBOARDING_REQUIRED, entity="allocation", error=why)
                _alert(session, f"onboard-{alloc.vendor_id}-{demand.id}",
                       f"onboarding needed -- {_vendor_name(alloc.vendor_id, session)} (bot vendor "
                       f"{alloc.vendor_id}) won {demand.part_no} x{alloc.quantity} (quote {alloc.quote_reference}), "
                       f"but {why}. The quote is saved and the PO-MST is created automatically once this is "
                       "fixed; it is NOT given to another vendor.", hours=24)
            alloc.next_try_at = now_ist_naive() + timedelta(minutes=60)
            session.flush()
            return
        _move(session, alloc, m.VENDOR_MAPPED, entity="allocation", supplier_dealer_id=dealer, error=None)

    if alloc.state == m.VENDOR_MAPPED:
        try:
            row, master = _current_row(demand, client, today)
        except PortalError as exc:
            _bump(session, alloc, exc)
            _dead(session, alloc, "allocation", label)
            return
        why = _not_allocatable(row, demand, alloc.quantity)
        if why:
            _move(session, alloc, m.ALLOCATION_CANCELLED, entity="allocation", error=f"not allocated: {why}")
            _maybe_close(session, demand)
            return
        planned = _int(row.get("planned_quantity")) or 0
        allocation_id = _int(master.get("allocation_id")) if master else None
        if allocation_id is not None:
            logger.info("Portal procurement: %s supersedes PO-MST allocation %s (%s).", alloc.quote_reference,
                        allocation_id, _first(master, "po_group_id", "master_po_group_id"))
        qty = min(alloc.quantity, planned)
        if qty < alloc.quantity:
            logger.info("Portal procurement: %s capped %d -> %d (Portal planned quantity).",
                        alloc.quote_reference, alloc.quantity, qty)
        # Commit the "submitting" state BEFORE the POST: if the process dies
        # or the answer is lost, the next run reconciles instead of re-posting.
        if not _move(session, alloc, m.ALLOCATION_SUBMITTING, entity="allocation", quantity=qty):
            return
        session.commit()
        try:
            body = client.allocate(_allocation_payload(alloc, demand, qty, allocation_id))
        except UnknownOutcome as exc:
            _move(session, alloc, m.ALLOCATION_UNKNOWN, entity="allocation", error=_err(exc),
                  http_status=exc.status, request_id=exc.request_id)
            session.commit()
            return
        except AuthStopped:
            # Nothing was written (auth failed before the Portal acted).
            _move(session, alloc, m.VENDOR_MAPPED, entity="allocation", error="auth stopped")
            session.commit()
            raise
        except PortalError as exc:
            if exc.status == 429:
                _move(session, alloc, m.VENDOR_MAPPED, entity="allocation", error=_err(exc), http_status=429)
                _bump(session, alloc, exc, minutes=5)
            else:
                _move(session, alloc, m.ALLOCATION_FAILED, entity="allocation", error=_err(exc),
                      http_status=exc.status, request_id=exc.request_id)
                _alert(session, f"alloc-fail-{alloc.id}", f"the Portal refused allocation {label}: {_err(exc)}",
                       hours=24)
            session.commit()
            return
        masters = _masters(body)
        if not masters:
            _move(session, alloc, m.ALLOCATION_UNKNOWN, entity="allocation",
                  error="allocate answered without masters -- reconciling")
            session.commit()
            return
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        _record_masters(session, alloc, masters, data.get("batch_id"))
        if _move(session, alloc, m.PO_MST_CREATED, entity="allocation", masters=masters,
                 batch_id=str(data.get("batch_id") or "") or None, error=None):
            demand.allocated_quantity = (demand.allocated_quantity or 0) + qty
            session.commit()
            _tell_po_mst_created(session, alloc, demand, masters)
        session.commit()
        return

    if alloc.state in (m.ALLOCATION_SUBMITTING, m.ALLOCATION_UNKNOWN):
        _reconcile_allocation(session, alloc, demand, client, today, label)


def _reconcile_allocation(session: Session, alloc: m.PortalAllocation, demand: m.PortalDemandLine,
                          client: PortalClient, today: date, label: str) -> None:
    """The POST outcome is unknown. Look for an allocation carrying OUR quote
    reference. Found -> it happened. Proven absent -> safe to allocate again."""
    if alloc.state == m.ALLOCATION_SUBMITTING and alloc.updated_at and \
            now_ist_naive() - alloc.updated_at < timedelta(minutes=2):
        return  # another worker may be mid-POST
    date_from, date_to = _window(today)
    if demand.order_date is not None and demand.order_date.isoformat() < date_from:
        date_from = demand.order_date.isoformat()
    try:
        body = client.active_with_po_mst(date_from, date_to, po_group_id=demand.source_po_group_id,
                                         part_no=demand.part_no)
    except PortalError as exc:
        _bump(session, alloc, exc)
        _dead(session, alloc, "allocation", label)
        return
    rows = _rows(body)
    ours = [r for r in rows if str(r.get("quote_reference") or "") == alloc.quote_reference]
    if ours:
        masters = []
        for r in ours:
            po = _first(r, "po_mst", "po_group_id")
            if po:
                masters.append({"po_mst": po, "vendor_id": _first(r, "supplier_dealer_id", "vendor_id"),
                                "vendor": _first(r, "supplier_dealer_name", "vendor"), "brand": r.get("brand"),
                                "quantity": _first(r, "planned_quantity", "quantity"),
                                "arrival_date": r.get("arrival_date")})
        if masters:
            _record_masters(session, alloc, masters, None)
            if _move(session, alloc, m.PO_MST_CREATED, entity="allocation", masters=masters,
                     error="reconciled: the allocation had been made"):
                demand.allocated_quantity = (demand.allocated_quantity or 0) + alloc.quantity
                session.commit()
                _tell_po_mst_created(session, alloc, demand, masters)
            session.commit()
            return
    # The Portal answered and shows no allocation with our reference: no side
    # effect happened, so the allocation may be submitted again.
    if rows or _total(body) == 0:
        _move(session, alloc, m.VENDOR_MAPPED, entity="allocation",
              error="reconciled: no allocation with this quote reference -- will resubmit")
        alloc.attempts = (alloc.attempts or 0) + 1
        session.commit()
        _dead(session, alloc, "allocation", label)
        return
    _bump(session, alloc, None, note="reconcile response could not be read")
    _dead(session, alloc, "allocation", label)


def _maybe_close(session: Session, demand: m.PortalDemandLine) -> None:
    open_allocs = session.execute(
        select(m.PortalAllocation.id).where(
            m.PortalAllocation.demand_line_id == demand.id,
            m.PortalAllocation.state.in_(_ALLOC_ACTIVE + (m.PO_MST_CREATED,)),
        )
    ).first()
    if not open_allocs and demand.state not in (m.CLOSED, m.COMPLETED):
        _move(session, demand, m.CLOSED, entity="demand", error="no longer pending on the Portal")


# ====================================================================== steps: PO-MST
_SAFE_KEYS = ("safe", "safe_matches", "matches", "safe_allocations", "allocations", "replaceable")
_BLOCKED_KEYS = ("blocked", "blocked_matches", "conflicts", "blocked_conflicts", "protected")
_FUTURE_KEYS = ("future", "future_stock", "excess", "future_excess", "excess_supply")


def _sum_items(items: Any) -> tuple[int, list[int]]:
    qty = 0
    ids: list[int] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            qty += _int(_first(item, "quantity", "qty", "knockoff_quantity", "matched_quantity")) or 0
            sid = _int(_first(item, "supply_order_line_id", "order_line_id", "line_id", "id"))
            if sid is not None:
                ids.append(sid)
    return qty, ids


def parse_preview(body: Any) -> dict | None:
    """Quantities and ids out of a knockoff preview -- by keys, never UI
    text. None when the shape cannot be read (a person then decides)."""
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return None
    found = False
    result = {"safe": 0, "blocked": 0, "future": 0, "safe_ids": [], "blocked_ids": [],
              "requires_confirmation": bool(data.get("requires_confirmation"))}
    for bucket, keys in (("safe", _SAFE_KEYS), ("blocked", _BLOCKED_KEYS), ("future", _FUTURE_KEYS)):
        for key in keys:
            for suffix in ("_quantity", "_qty"):
                value = _int(data.get(key + suffix)) if (key + suffix) in data else None
                if value is not None:
                    result[bucket] = max(result[bucket], value)
                    found = True
            if key in data:
                value = data[key]
                if isinstance(value, list):
                    qty, ids = _sum_items(value)
                    result[bucket] = max(result[bucket], qty)
                    if bucket == "safe":
                        result["safe_ids"] = sorted(set(result["safe_ids"]) | set(ids))
                    if bucket == "blocked":
                        result["blocked_ids"] = sorted(set(result["blocked_ids"]) | set(ids))
                    found = True
                elif _int(value) is not None:
                    result[bucket] = max(result[bucket], _int(value))
                    found = True
    if "requires_confirmation" in data:
        found = True
    if not found:
        return None
    # Never include a blocked line in a safe commit.
    result["safe_ids"] = [i for i in result["safe_ids"] if i not in set(result["blocked_ids"])]
    return result


def _po_label(po: m.PortalPoMaster) -> str:
    return f"{po.po_mst} ({po.vendor_name or po.vendor_dealer_id})"


def _quote_refs(session: Session, po: m.PortalPoMaster) -> list[str]:
    rows = session.execute(
        select(m.PortalAllocation.quote_reference).where(m.PortalAllocation.id.in_(po.allocation_ids or [0]))
    ).scalars().all()
    return list(rows)


def _step_po(session: Session, po: m.PortalPoMaster, client: PortalClient) -> None:
    refs = ", ".join(_quote_refs(session, po)) or "-"
    label = _po_label(po)

    if po.state == m.PO_MST_CREATED:
        try:
            body = client.knockoff_preview(po.po_mst)
        except PortalError as exc:  # preview has no side effect, so it may be retried
            _bump(session, po, exc)
            _dead(session, po, "po_mst", label)
            return
        parsed = parse_preview(body)
        if parsed is None:
            _move(session, po, m.NEEDS_REVIEW, entity="po_mst", error="knockoff preview could not be read",
                  preview=body if isinstance(body, dict) else {"raw": str(body)[:2000]})
            _alert(session, f"preview-{po.po_mst}", f"{label}: the knockoff preview could not be read. "
                   "Open it on the Portal and decide; the bot will not post it.", hours=24)
            return
        _move(session, po, m.KNOCKOFF_PREVIEWED, entity="po_mst", preview=parsed, safe_qty=parsed["safe"],
              blocked_qty=parsed["blocked"], future_qty=parsed["future"], safe_line_ids=parsed["safe_ids"])
        session.commit()

    if po.state == m.KNOCKOFF_PREVIEWED:
        safe, blocked, future = po.safe_qty or 0, po.blocked_qty or 0, po.future_qty or 0
        needs_approval = blocked > 0 or future > 0 or (
            safe == 0 and (po.preview or {}).get("requires_confirmation")
        )
        commit_safe = safe > 0 and future == 0 or (safe > 0 and blocked > 0)
        if commit_safe:
            if not po.safe_line_ids:
                _move(session, po, m.NEEDS_REVIEW, entity="po_mst",
                      error="safe quantity without safe line ids -- not committed")
                _alert(session, f"safe-ids-{po.po_mst}", f"{label}: the preview shows safe knockoff but no line "
                       "ids, so nothing was committed. Check it on the Portal.", hours=24)
                return
            try:
                client.knockoff_commit(
                    po.po_mst, list(po.safe_line_ids),
                    f"Vendor bot accepted quickest confirmed supply; quote {refs}",
                )
            except UnknownOutcome as exc:
                _bump(session, po, exc, note=f"safe commit outcome unknown: {_err(exc)}")
                _alert(session, f"commit-unknown-{po.po_mst}", f"{label}: safe knockoff commit got no answer. "
                       "Check it on the Portal before the bot continues.", hours=24)
                _move(session, po, m.NEEDS_REVIEW, entity="po_mst", error=po.error)
                return
            except PortalError as exc:
                _move(session, po, m.NEEDS_REVIEW, entity="po_mst", error=_err(exc), http_status=exc.status,
                      request_id=exc.request_id)
                _alert(session, f"commit-{po.po_mst}", f"{label}: safe knockoff commit refused: {_err(exc)}",
                       hours=24)
                return
            if not needs_approval:
                _move(session, po, m.SAFE_KNOCKOFF_COMMITTED, entity="po_mst", error=None)
                session.commit()
        if needs_approval:
            _request_approval(session, po, client, refs, label)
            return
        if po.state == m.KNOCKOFF_PREVIEWED:
            # Nothing to knock off, nothing blocked: straight on to Odoo.
            _move(session, po, m.SAFE_KNOCKOFF_COMMITTED, entity="po_mst", error="no knockoff needed")
            session.commit()

    if po.state == m.APPROVAL_PENDING:
        try:
            status = client.duplicate_approval_status(po.po_mst)
        except PortalError as exc:
            _bump(session, po, exc, minutes=5)
            return
        state = _approval_state(status)
        if state == "approved":
            _move(session, po, m.APPROVAL_APPROVED, entity="po_mst", approval_status="approved",
                  actor="super_admin")
            session.commit()
        elif state == "rejected":
            inner = status.get("approval") if isinstance(status, dict) and isinstance(status.get("approval"), dict) else status
            reason = str(_first(inner if isinstance(inner, dict) else {}, "admin_remark", "reason", "decision_reason",
                                "admin_reason", "remarks") or "")[:500]
            _move(session, po, m.APPROVAL_REJECTED, entity="po_mst", approval_status="rejected",
                  approval_reason=reason or None, actor="super_admin", error=reason or "rejected")
            _on_rejected(session, po, client, reason)
            session.commit()
        else:
            po.next_try_at = now_ist_naive() + timedelta(minutes=settings.step_minutes)
            session.flush()
        return

    if po.state in (m.SAFE_KNOCKOFF_COMMITTED, m.APPROVAL_APPROVED):
        if not _move(session, po, m.ODOO_POSTING, entity="po_mst"):
            return
        session.commit()
        try:
            body = client.push_to_odoo(po.po_mst, po.vendor_dealer_id)
        except UnknownOutcome as exc:
            _bump(session, po, exc, note=f"Odoo push outcome unknown: {_err(exc)} -- reconciling", minutes=2)
            session.commit()
            return
        except AuthStopped:
            raise
        except PortalError as exc:
            _move(session, po, m.NEEDS_REVIEW, entity="po_mst", error=_err(exc), http_status=exc.status,
                  request_id=exc.request_id)
            _alert(session, f"odoo-{po.po_mst}", f"{label}: push to Odoo refused (HTTP {exc.status}): "
                   f"{exc.detail or exc}. Fix it on the Portal (vendor/Odoo partner/product/branch/margin/"
                   "approval); the bot will not retry by itself.", hours=24)
            session.commit()
            return
        ref, name = _odoo_ref(body)
        if ref or name:
            _move(session, po, m.ODOO_POSTED, entity="po_mst", odoo_ref=str(ref or name), odoo_name=name, error=None)
        else:
            _bump(session, po, None, note="Odoo push answered without a PO reference -- reconciling", minutes=2)
        session.commit()
        return

    if po.state == m.ODOO_POSTING:
        if po.updated_at and now_ist_naive() - po.updated_at < timedelta(minutes=2):
            return
        try:
            body = client.purchase_order(po.po_mst)
        except PortalError as exc:
            _bump(session, po, exc, minutes=5)
            _dead(session, po, "po_mst", label)
            return
        ref, name = _odoo_ref(body)
        if ref or name:
            _move(session, po, m.ODOO_POSTED, entity="po_mst", odoo_ref=str(ref or name), odoo_name=name,
                  error="reconciled: Odoo PO exists")
            session.commit()
            return
        if _odoo_absent(body):
            # Proven not pushed: back to ready, the push may go again.
            ready = m.APPROVAL_APPROVED if po.approval_status == "approved" else m.SAFE_KNOCKOFF_COMMITTED
            po.attempts = (po.attempts or 0) + 1
            _move(session, po, ready, entity="po_mst", error="reconciled: no Odoo PO yet -- will push again")
            session.commit()
            _dead(session, po, "po_mst", label)
            return
        _bump(session, po, None, note="could not tell whether the Odoo PO exists", minutes=10)
        _dead(session, po, "po_mst", label)
        return

    if po.state == m.ODOO_POSTED:
        from backend.app.portal_procurement import notify

        ok, why, digest, size = notify.fetch_and_check_pdf(session, po, client)
        if ok:
            _move(session, po, m.PDF_DOWNLOADED, entity="po_mst", pdf_sha256=digest, pdf_size=size, error=None)
            session.commit()
        else:
            _bump(session, po, None, note=why)
            if not _dead(session, po, "po_mst", label) and (po.attempts or 0) >= 2:
                _alert(session, f"pdf-{po.po_mst}", f"{label}: PO PDF failed the checks ({why}).", hours=12)
            return

    if po.state == m.PDF_DOWNLOADED:
        from backend.app.portal_procurement import notify

        if notify.send_po(session, po):
            _move(session, po, m.PURCHASE_USER_NOTIFIED, entity="po_mst")
            session.commit()
        else:
            _bump(session, po, None, note="WhatsApp delivery failed", minutes=10)
            _dead(session, po, "po_mst", label)
            return

    if po.state == m.PURCHASE_USER_NOTIFIED:
        if po.notified:  # the PDF itself reached at least one purchase user
            _move(session, po, m.COMPLETED, entity="po_mst")
            _complete_demands(session, po)
            session.commit()


def _request_approval(session: Session, po: m.PortalPoMaster, client: PortalClient, refs: str, label: str) -> None:
    # Never a second request while one is pending.
    try:
        existing = _approval_state(client.duplicate_approval_status(po.po_mst))
    except PortalError as exc:
        existing = None if exc.status == 404 else "error"
        if existing == "error":
            _bump(session, po, exc, minutes=5)
            return
    if existing in ("pending", "approved", "rejected"):
        _move(session, po, m.APPROVAL_PENDING, entity="po_mst", approval_status=existing)
        session.commit()
        return
    reason = (
        f"Vendor bot found fastest confirmed stock. Blocked/future quantity requires Super Admin review "
        f"(blocked {po.blocked_qty or 0}, future {po.future_qty or 0}). Quote {refs}."
    )
    try:
        client.request_duplicate_approval(po.po_mst, reason)
    except UnknownOutcome as exc:
        # Reconciled on the next step by reading the approval status.
        _bump(session, po, exc, note=f"approval request outcome unknown: {_err(exc)}", minutes=2)
        return
    except PortalError as exc:
        _move(session, po, m.NEEDS_REVIEW, entity="po_mst", error=_err(exc), http_status=exc.status,
              request_id=exc.request_id)
        _alert(session, f"approval-{po.po_mst}", f"{label}: duplicate approval request refused: {_err(exc)}",
               hours=24)
        return
    _move(session, po, m.APPROVAL_PENDING, entity="po_mst", approval_status="pending")
    session.commit()
    _alert(session, f"approval-asked-{po.po_mst}", f"{label} needs Super Admin duplicate approval on the "
           f"Dealer Portal (blocked {po.blocked_qty or 0}, future {po.future_qty or 0}).", hours=24)


def _approval_state(body: Any) -> str | None:
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    # The live Portal answers {"approval": {"status": "pending", ...}} (16 Nov 2026).
    if isinstance(data, dict) and isinstance(data.get("approval"), dict):
        data = data["approval"]
    if not isinstance(data, dict):
        return None
    value = str(_first(data, "status", "approval_status", "decision") or "").lower()
    if value in ("approved", "approve"):
        return "approved"
    if value in ("rejected", "reject", "declined"):
        return "rejected"
    if value in ("pending", "requested", "awaiting"):
        return "pending"
    return None


def _on_rejected(session: Session, po: m.PortalPoMaster, client: PortalClient, reason: str) -> None:
    """Super Admin said no: never push it. The allocations are marked, and
    each source line still on the Portal goes back for vendor selection."""
    reopened: list[str] = []
    for alloc in session.execute(
        select(m.PortalAllocation).where(m.PortalAllocation.id.in_(po.allocation_ids or [0]))
    ).scalars().all():
        _move(session, alloc, m.ALLOCATION_CANCELLED, entity="allocation", actor="super_admin",
              error=f"duplicate approval rejected: {reason or '-'}")
        demand = session.get(m.PortalDemandLine, alloc.demand_line_id)
        if demand is None:
            continue
        demand.allocated_quantity = max(0, (demand.allocated_quantity or 0) - alloc.quantity)
        try:
            row, _master = _current_row(demand, client, now_ist_naive().date())
        except PortalError:
            row = None
        if row is not None and (_int(row.get("planned_quantity")) or 0) > 0:
            demand.planned_quantity = _int(row.get("planned_quantity")) or demand.planned_quantity
            _move(session, demand, m.DISCOVERED, entity="demand", actor="super_admin",
                  error=f"approval rejected for {po.po_mst}: reopened", advance_order_id=None)
            reopened.append(demand.part_no)
    _alert(session, f"rejected-{po.po_mst}",
           f"{_po_label(po)}: Super Admin REJECTED duplicate procurement ({reason or 'no reason given'}). "
           f"Not sent to Odoo. {('Reopened for vendors: ' + ', '.join(reopened)) if reopened else ''}", hours=48)


def _odoo_ref(body: Any) -> tuple[Any, str | None]:
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return None, None
    ref = _first(data, "odoo_po_id", "odoo_id", "odoo_purchase_order_id", "odoo_ref")
    name = _first(data, "odoo_po_name", "odoo_name", "odoo_po_number", "odoo_reference")
    return ref, (str(name) if name else None)


def _odoo_absent(body: Any) -> bool:
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return False
    known = any(k in data for k in ("odoo_po_id", "odoo_po_name", "odoo_ref", "odoo_status", "pushed_to_odoo"))
    if not known:
        return False
    return not (_odoo_ref(data)[0] or _odoo_ref(data)[1]) and not data.get("pushed_to_odoo")


def _complete_demands(session: Session, po: m.PortalPoMaster) -> None:
    for alloc in session.execute(
        select(m.PortalAllocation).where(m.PortalAllocation.id.in_(po.allocation_ids or [0]))
    ).scalars().all():
        demand = session.get(m.PortalDemandLine, alloc.demand_line_id)
        if demand is None or demand.state in (m.COMPLETED, m.CLOSED):
            continue
        if (demand.allocated_quantity or 0) < (demand.planned_quantity or 0):
            continue  # part still being sourced from other vendors
        pos = [
            r for r in session.execute(select(m.PortalPoMaster)).scalars().all()
            if set(r.allocation_ids or []) & set(_alloc_ids(session, demand))
        ]
        if all(r.state == m.COMPLETED for r in pos):
            _move(session, demand, m.COMPLETED, entity="demand")


def _alloc_ids(session: Session, demand: m.PortalDemandLine) -> list[int]:
    return list(session.execute(
        select(m.PortalAllocation.id).where(m.PortalAllocation.demand_line_id == demand.id)
    ).scalars().all())


# ====================================================================== runners
def run_steps(session: Session, client: PortalClient, today: date | None = None) -> None:
    today = today or now_ist_naive().date()
    collect(session)
    session.commit()
    for alloc in session.execute(
        select(m.PortalAllocation).where(m.PortalAllocation.state.in_(_ALLOC_ACTIVE)).order_by(m.PortalAllocation.id)
    ).scalars().all():
        if _due(alloc):
            _step_allocation(session, alloc, client, today)
            session.commit()
    for po in session.execute(
        select(m.PortalPoMaster).where(m.PortalPoMaster.state.in_(_PO_ACTIVE)).order_by(m.PortalPoMaster.id)
    ).scalars().all():
        if _due(po):
            _step_po(session, po, client)
            session.commit()


def _client() -> PortalClient:
    return PortalClient()


def poll_job() -> None:
    """Scheduler: read the Portal, then send new lines to vendors."""
    from core.db import get_session

    from backend.app.portal_procurement import client as portal_client

    if not settings.enabled or portal_client.blocked():
        return
    with get_session() as session:
        if not settings.configured():
            # Logged only -- no daily WhatsApp to the team while the Portal
            # team has not issued the service account yet.
            logger.warning("Portal procurement is on but PORTAL_PROCUREMENT_USERNAME/_PASSWORD are not set; "
                           "nothing is read from the Portal.")
            return
        client = _client()
        try:
            poll(session, client)
            session.commit()
            if settings.rounds:
                # Only at a round time (09:30, 12:30, 15:30, 18:30): every open
                # line goes out together; between rounds new lines wait.
                from backend.app.portal_procurement import rounds

                slot = rounds.round_due(session)
                if slot is not None:
                    sent = enquire(session, client)
                    rounds.mark_round(session, slot, sent)
                    session.commit()
            else:
                enquire(session, client)
        except AuthStopped as exc:
            session.rollback()
            _alert(session, "auth", f"run stopped -- {exc}. Check the service account / Cloudflare token.", hours=6)
        finally:
            client.close()


def steps_job() -> None:
    """Scheduler: move every allocation and PO-MST one step on."""
    from core.db import get_session

    from backend.app.portal_procurement import client as portal_client

    if not settings.enabled or not settings.configured() or portal_client.blocked():
        return
    with get_session() as session:
        client = _client()
        try:
            run_steps(session, client)
        except AuthStopped as exc:
            session.rollback()
            _alert(session, "auth", f"run stopped -- {exc}. Check the service account / Cloudflare token.", hours=6)
        finally:
            client.close()


def summary(session: Session) -> dict:
    """Counts by state + the latest rows, for the console."""
    from sqlalchemy import func

    def counts(model) -> dict:
        return dict(session.execute(select(model.state, func.count()).group_by(model.state)).all())

    recent = session.execute(select(m.PortalPoMaster).order_by(m.PortalPoMaster.id.desc()).limit(20)).scalars().all()
    return {
        "enabled": settings.enabled,
        "configured": settings.configured(),
        "demand": counts(m.PortalDemandLine),
        "allocations": counts(m.PortalAllocation),
        "po_masters": counts(m.PortalPoMaster),
        "recent_po_masters": [
            {"po_mst": p.po_mst, "state": p.state, "vendor": p.vendor_name, "odoo": p.odoo_name or p.odoo_ref,
             "error": p.error, "updated_at": p.updated_at.isoformat() if p.updated_at else None}
            for p in recent
        ],
    }
