"""The advance-order engine.

    create_order()   the sales bot's request -> lines grouped by brand -> the
                     first vendor for each brand is queued
    tick()           (scheduler, every few minutes) sends queued questions
                     inside vendor hours, and moves past a vendor who has not
                     answered within ADVANCE_ORDER_VENDOR_WAIT_MINUTES
    handle_vendor_text()
                     a WhatsApp text from a number with an open question ->
                     read it -> lines answered -> anything still unanswered
                     goes to the next vendor for that brand
    confirm_order()  the customer said yes -> each vendor who has the parts
                     gets the order; admins + purchase team are told
    cancel_order()

Vendors for a brand come from `vendor_brands` (priority 1 first); a brand
with no rows uses brand "*". Own-stock "vendors" and inactive vendors are
never asked. A vendor is asked at most once per order and brand.

Sending goes through `_send_text` / `_send_template`, which tests replace."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from backend.app.advance_orders.config import advance_order_settings as cfg
from backend.app.advance_orders.parser import parse_vendor_reply
from core.logging_setup import get_logger
from core.models import Vendor
from core.time_utils import now_ist_naive

logger = get_logger(__name__)


# ------------------------------------------------------------------ sending
def _send_text(to: str, body: str) -> None:
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    WhatsAppClient(whatsapp_settings).send_text_message(to, body)


def _send_template(to: str, name: str, language: str, params: list[str]) -> None:
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    WhatsAppClient(whatsapp_settings).send_template_message(to, name, language, params)


def _internal_numbers(session: Session) -> list[str]:
    from backend.app.integrations.whatsapp.recipients import internal_file_recipients

    return internal_file_recipients(session)


def _vendor_numbers(vendor_id: int, session: Session) -> list[str]:
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber

    return [
        n
        for n in session.execute(
            select(WhatsAppRegisteredNumber.whatsapp_number).where(WhatsAppRegisteredNumber.vendor_id == vendor_id)
        ).scalars()
        if n
    ]


def _normalize(number: str) -> str:
    from backend.app.integrations.whatsapp.registry import normalize_number

    return normalize_number(number)


def _tell_internal(session: Session, body: str) -> None:
    for number in _internal_numbers(session):
        try:
            _send_text(number, body)
        except Exception:  # noqa: BLE001 -- one failed alert must not stop the rest
            logger.exception("Advance order: could not alert %s.", number)


# ------------------------------------------------------------------ helpers
def _brand(value: str | None) -> str:
    b = str(value or "").strip().upper()
    return b or cfg.default_brand


def _fmt_day(d: date | None) -> str:
    return d.strftime("%d %b").lstrip("0") if d else ""


def _in_vendor_hours(now: datetime) -> bool:
    try:
        start_text, end_text = cfg.vendor_hours.split("-")
        sh, sm = (int(x) for x in start_text.strip().split(":"))
        eh, em = (int(x) for x in end_text.strip().split(":"))
    except ValueError:
        return True
    minutes = now.hour * 60 + now.minute
    return sh * 60 + sm <= minutes <= eh * 60 + em


def vendors_for_brand(brand: str, session: Session) -> list[Vendor]:
    """The vendors to ask for this brand, first to last."""

    def rows(b: str) -> list[Vendor]:
        return list(
            session.execute(
                select(Vendor)
                .join(m.VendorBrand, m.VendorBrand.vendor_id == Vendor.id)
                .where(m.VendorBrand.brand == b, m.VendorBrand.active.is_(True), Vendor.active.is_(True))
                .where(Vendor.is_own_stock.is_(False))
                .order_by(m.VendorBrand.priority, Vendor.name)
            ).scalars()
        )

    found = rows(_brand(brand))
    if not found and _brand(brand) != cfg.default_brand:
        found = rows(cfg.default_brand)
    return found


def _brand_for_line(part_number: str, given: str | None, session: Session) -> str:
    if given:
        return _brand(given)
    from core.models import Part

    part = session.execute(select(Part).where(Part.canonical_part_number == part_number)).scalar_one_or_none()
    return _brand(part.brand if part is not None and part.brand else None)


# ------------------------------------------------------------------ create
def create_order(payload: dict, session: Session) -> m.AdvanceOrder:
    ref = str(payload.get("external_ref") or "").strip() or None
    if ref:
        existing = session.execute(select(m.AdvanceOrder).where(m.AdvanceOrder.external_ref == ref)).scalar_one_or_none()
        if existing is not None:
            return existing
    customer = payload.get("customer") or {}
    needed = payload.get("needed_by")
    order = m.AdvanceOrder(
        external_ref=ref,
        source=str(payload.get("source") or "autoflow"),
        customer_portal_id=str(customer.get("portal_id")) if customer.get("portal_id") is not None else None,
        customer_name=customer.get("name"),
        customer_phone=customer.get("phone"),
        requested_by=payload.get("requested_by"),
        needed_by=date.fromisoformat(needed) if needed else None,
        status=m.ASKING,
    )
    for line in payload.get("lines") or []:
        part = str(line.get("part_number") or "").strip().upper()
        qty = int(line.get("qty") or 0)
        if not part or qty <= 0:
            continue
        order.lines.append(
            m.AdvanceOrderLine(
                part_number=part,
                part_name=line.get("part_name"),
                brand=_brand_for_line(part, line.get("brand"), session),
                qty=qty,
            )
        )
    if not order.lines:
        raise ValueError("no usable lines (part_number and qty > 0 are required)")
    session.add(order)
    session.flush()
    logger.info("Advance order %s (%s) created: %d line(s).", order.id, ref, len(order.lines))
    _advance(order, session, now_ist_naive())
    return order


# ------------------------------------------------------------------ the loop
def _open_query_for_brand(order: m.AdvanceOrder, brand: str, session: Session) -> m.AdvanceVendorQuery | None:
    return session.execute(
        select(m.AdvanceVendorQuery).where(
            m.AdvanceVendorQuery.advance_order_id == order.id,
            m.AdvanceVendorQuery.brand == brand,
            m.AdvanceVendorQuery.status.in_([m.Q_QUEUED, m.Q_SENT]),
        )
    ).scalar_one_or_none()


def _asked_vendor_ids(order: m.AdvanceOrder, brand: str, session: Session) -> set[int]:
    return set(
        session.execute(
            select(m.AdvanceVendorQuery.vendor_id).where(
                m.AdvanceVendorQuery.advance_order_id == order.id, m.AdvanceVendorQuery.brand == brand
            )
        ).scalars()
    )


def _advance(order: m.AdvanceOrder, session: Session, now: datetime) -> None:
    """For every brand with unanswered lines and no open question: queue the
    next vendor, or -- when nobody is left -- mark those lines unavailable.
    Then send what can be sent and settle the order's status."""
    if order.status not in (m.ASKING,):
        return
    waiting: dict[str, list[m.AdvanceOrderLine]] = {}
    for line in order.lines:
        if line.status == m.LINE_ASKING:
            waiting.setdefault(line.brand, []).append(line)
    for brand, lines in waiting.items():
        if _open_query_for_brand(order, brand, session) is not None:
            continue
        asked = _asked_vendor_ids(order, brand, session)
        nxt = next((v for v in vendors_for_brand(brand, session) if v.id not in asked), None)
        if nxt is None:
            for line in lines:
                line.status = m.LINE_UNAVAILABLE
                line.note = "no vendor had it" if asked else "no vendor listed for this brand"
            if not asked:
                _tell_internal(
                    session,
                    f"Advance order #{order.id}: brand {brand} ka koi vendor list mein nahi hai "
                    f"({', '.join(line.part_number for line in lines)}). vendor_brands mein daaliye.",
                )
            continue
        session.add(
            m.AdvanceVendorQuery(
                advance_order_id=order.id,
                brand=brand,
                vendor_id=nxt.id,
                line_ids=[line.id for line in lines],
                status=m.Q_QUEUED,
            )
        )
    session.flush()
    _send_queued(order, session, now)
    _settle(order)


def _question_text(order: m.AdvanceOrder, lines: list[m.AdvanceOrderLine]) -> str:
    rows = "\n".join(f"{i + 1}. {line.part_number} x{line.qty}" for i, line in enumerate(lines))
    by = f"\n{_fmt_day(order.needed_by)} tak chahiye." if order.needed_by else ""
    return (
        "Namaste, Cartrends purchase desk se.\n"
        f"Ye parts mil jayenge? Kab tak aa sakte hain?\n{rows}{by}\n\n"
        "Jaise: \"haan 5 din\", \"nahi\", ya har part ke aage likh dijiye."
    )


def _send_queued(order: m.AdvanceOrder, session: Session, now: datetime) -> None:
    queued = session.execute(
        select(m.AdvanceVendorQuery).where(
            m.AdvanceVendorQuery.advance_order_id == order.id, m.AdvanceVendorQuery.status == m.Q_QUEUED
        )
    ).scalars().all()
    if not queued or not _in_vendor_hours(now):
        return
    by_id = {line.id: line for line in order.lines}
    for q in queued:
        lines = [by_id[i] for i in q.line_ids if i in by_id and by_id[i].status == m.LINE_ASKING]
        if not lines:
            q.status = m.Q_CLOSED
            continue
        numbers = _vendor_numbers(q.vendor_id, session)
        delivered: list[str] = []
        for number in numbers:
            try:
                if cfg.vendor_template:
                    _send_template(
                        number,
                        cfg.vendor_template,
                        cfg.template_language,
                        [
                            q.brand if q.brand != cfg.default_brand else "parts",
                            "; ".join(f"{line.part_number} x{line.qty}" for line in lines),
                            _fmt_day(order.needed_by) or "jaldi",
                        ],
                    )
                else:
                    _send_text(number, _question_text(order, lines))
                delivered.append(_normalize(number))
            except Exception:  # noqa: BLE001 -- the next number / vendor is tried
                logger.exception("Advance order %s: question to vendor %s at %s failed.", order.id, q.vendor_id, number)
        q.numbers = delivered
        if delivered:
            q.status = m.Q_SENT
            q.sent_at = now
            q.deadline_at = now + timedelta(minutes=cfg.vendor_wait_minutes)
            logger.info("Advance order %s: asked vendor %s about %s.", order.id, q.vendor_id, q.brand)
        else:
            q.status = m.Q_FAILED
            logger.warning("Advance order %s: vendor %s could not be messaged (no number / send failed).", order.id, q.vendor_id)
    session.flush()
    # A vendor who could not be reached is skipped straight away.
    if any(q.status == m.Q_FAILED for q in queued):
        _advance(order, session, now)


def _settle(order: m.AdvanceOrder) -> None:
    if order.status != m.ASKING:
        return
    if any(line.status == m.LINE_ASKING for line in order.lines):
        return
    order.status = m.QUOTED if any(line.status == m.LINE_AVAILABLE for line in order.lines) else m.NO_VENDOR
    logger.info("Advance order %s is %s.", order.id, order.status)


def tick(session: Session, now: datetime | None = None) -> None:
    """Send what is queued (inside vendor hours) and move past silent vendors."""
    now = now or now_ist_naive()
    overdue = session.execute(
        select(m.AdvanceVendorQuery).where(m.AdvanceVendorQuery.status == m.Q_SENT, m.AdvanceVendorQuery.deadline_at < now)
    ).scalars().all()
    for q in overdue:
        q.status = m.Q_TIMEOUT
        logger.info("Advance order %s: vendor %s did not answer in time.", q.advance_order_id, q.vendor_id)
    session.flush()
    for order in session.execute(select(m.AdvanceOrder).where(m.AdvanceOrder.status == m.ASKING)).scalars():
        _advance(order, session, now)


# ------------------------------------------------------------------ replies
def handle_vendor_text(sender: str, text: str, session: Session, now: datetime | None = None) -> bool:
    """True when this text was a reply to an open advance-order question (and
    so must not be handled as anything else)."""
    now = now or now_ist_naive()
    number = _normalize(sender)
    if not number:
        return False
    open_queries = session.execute(
        select(m.AdvanceVendorQuery)
        .where(m.AdvanceVendorQuery.status == m.Q_SENT)
        .order_by(m.AdvanceVendorQuery.sent_at.desc())
    ).scalars().all()
    q = next((x for x in open_queries if number in (x.numbers or [])), None)
    if q is None:
        return False
    order = session.get(m.AdvanceOrder, q.advance_order_id)
    if order is None or order.status != m.ASKING:
        q.status = m.Q_CLOSED
        return False
    by_id = {line.id: line for line in order.lines}
    lines = [by_id[i] for i in q.line_ids if i in by_id and by_id[i].status == m.LINE_ASKING]
    answers = parse_vendor_reply(text, [(line.id, line.part_number, line.qty) for line in lines], now.date())
    if not answers:
        vendor = session.get(Vendor, q.vendor_id)
        _tell_internal(
            session,
            f"Advance order #{order.id}: {vendor.name if vendor else 'vendor'} ka jawab samajh nahi aaya:\n\"{text[:300]}\"\n"
            f"Parts: {', '.join(line.part_number + ' x' + str(line.qty) for line in lines)}",
        )
        return True
    q.status = m.Q_REPLIED
    q.replied_at = now
    q.reply_text = text[:1000]
    for line in lines:
        ans = answers.get(line.id)
        if ans is None:
            continue  # not answered -> the next vendor is asked about it
        if ans.available:
            line.status = m.LINE_AVAILABLE
            line.vendor_id = q.vendor_id
            line.available_qty = min(ans.available_qty, line.qty) if ans.available_qty else line.qty
            line.eta_date = ans.eta
        # "nahi": stays asking, so the next vendor for the brand is asked
        # about it; only when nobody is left is it marked unavailable.
    session.flush()
    _advance(order, session, now)
    return True


# ------------------------------------------------------------------ customer decision
def confirm_order(order: m.AdvanceOrder, line_ids: list[int] | None, session: Session) -> m.AdvanceOrder:
    if order.status != m.QUOTED:
        raise ValueError(f"order is {order.status}, only a quoted order can be confirmed")
    wanted = set(line_ids or [])
    by_vendor: dict[int, list[m.AdvanceOrderLine]] = {}
    for line in order.lines:
        if line.status != m.LINE_AVAILABLE:
            continue
        if wanted and line.id not in wanted:
            line.status = m.LINE_CANCELLED
            continue
        line.status = m.LINE_ORDERED
        by_vendor.setdefault(line.vendor_id, []).append(line)
    if not by_vendor:
        raise ValueError("none of those lines is available")
    now = now_ist_naive()
    order.status = m.CONFIRMED
    order.confirmed_at = now
    summary: list[str] = []
    for vendor_id, lines in by_vendor.items():
        vendor = session.get(Vendor, vendor_id)
        rows = "\n".join(
            f"{i + 1}. {line.part_number} x{line.available_qty or line.qty}"
            + (f" - {_fmt_day(line.eta_date)} tak" if line.eta_date else "")
            for i, line in enumerate(lines)
        )
        body = f"Order pakka (Cartrends ref ADV-{order.id}):\n{rows}\n\nBataye hue time pe bhej dijiye. Dhanyavaad."
        for number in _vendor_numbers(vendor_id, session):
            try:
                _send_text(number, body)
            except Exception:  # noqa: BLE001
                logger.exception("Advance order %s: order to vendor %s at %s failed.", order.id, vendor_id, number)
        summary.append(f"{vendor.name if vendor else vendor_id}:\n{rows}")
    _tell_internal(
        session,
        f"Advance order #{order.id} CONFIRMED - {order.customer_name or order.customer_phone or 'customer'}"
        + (f" ({_fmt_day(order.needed_by)} tak)" if order.needed_by else "")
        + "\n\n"
        + "\n\n".join(summary),
    )
    logger.info("Advance order %s confirmed with %d vendor(s).", order.id, len(by_vendor))
    return order


def cancel_order(order: m.AdvanceOrder, session: Session) -> m.AdvanceOrder:
    if order.status == m.CONFIRMED:
        raise ValueError("a confirmed order cannot be cancelled here - the vendors already have it")
    order.status = m.CANCELLED
    for line in order.lines:
        if line.status in (m.LINE_ASKING, m.LINE_AVAILABLE):
            line.status = m.LINE_CANCELLED
    for q in session.execute(
        select(m.AdvanceVendorQuery).where(
            m.AdvanceVendorQuery.advance_order_id == order.id,
            m.AdvanceVendorQuery.status.in_([m.Q_QUEUED, m.Q_SENT]),
        )
    ).scalars():
        q.status = m.Q_CLOSED
    return order


# ------------------------------------------------------------------ admin: by hand
def set_line_answer(
    order: m.AdvanceOrder,
    line_id: int,
    available: bool,
    session: Session,
    *,
    vendor_id: int | None = None,
    available_qty: int | None = None,
    eta: date | None = None,
) -> m.AdvanceOrder:
    """An admin who got the answer on the phone records it here."""
    line = next((x for x in order.lines if x.id == line_id), None)
    if line is None:
        raise ValueError("no such line")
    if order.status != m.ASKING:
        raise ValueError(f"order is {order.status}")
    line.status = m.LINE_AVAILABLE if available else m.LINE_UNAVAILABLE
    line.vendor_id = vendor_id if available else None
    line.available_qty = (min(available_qty, line.qty) if available_qty else line.qty) if available else None
    line.eta_date = eta if available else None
    line.note = "entered by admin"
    session.flush()
    _settle(order)
    return order


def order_out(order: m.AdvanceOrder, session: Session) -> dict:
    """What the sales bot reads. Vendor names are included for the desk; the
    sales bot never shows them to a customer."""
    vendors = {v.id: v.name for v in session.execute(select(Vendor)).scalars()} if order.lines else {}
    return {
        "id": order.id,
        "external_ref": order.external_ref,
        "status": order.status,
        "customer": {"portal_id": order.customer_portal_id, "name": order.customer_name, "phone": order.customer_phone},
        "needed_by": order.needed_by.isoformat() if order.needed_by else None,
        "confirmed_at": order.confirmed_at.isoformat() if order.confirmed_at else None,
        "lines": [
            {
                "id": line.id,
                "part_number": line.part_number,
                "part_name": line.part_name,
                "brand": line.brand,
                "qty": line.qty,
                "status": line.status,
                "available_qty": line.available_qty,
                "eta_date": line.eta_date.isoformat() if line.eta_date else None,
                "vendor_name": vendors.get(line.vendor_id) if line.vendor_id else None,
                "note": line.note,
            }
            for line in order.lines
        ],
    }
