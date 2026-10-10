"""The advance-order engine.

    create_order()   the sales bot's request -> lines grouped by brand -> the
                     first vendor for each brand is queued
    tick()           (scheduler, every few minutes) sends queued questions
                     inside vendor hours, moves past a vendor who has not
                     answered within ADVANCE_ORDER_VENDOR_WAIT_MINUTES, and
                     closes quote windows that have run out
    handle_vendor_text()
                     a WhatsApp text from a number with an open question ->
                     read it -> a QUOTE is stored per line -> anything still
                     unanswered goes to the next vendor for that brand

Several vendors are asked about one brand at once (ADVANCE_ORDER_QUOTE_FANOUT),
their answers are collected as `AdvanceVendorQuote` rows, and `ranking.py`
picks the winner once they have all answered or the quote window closes. Set
the fan-out to 1 to get the original first-vendor-wins behaviour back.
    confirm_order()  the customer said yes -> each vendor who has the parts
                     gets the order; admins + purchase team are told
    cancel_order()

Vendors for a brand come from `vendor_brands` (priority 1 first); a brand
with no rows uses brand "*". Own-stock "vendors" and inactive vendors are
never asked. A vendor is asked at most once per order and brand.

Sending goes through `_send_text` / `_send_template`, which tests replace."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import callback, escalation, models as m, quotes, ranking
from backend.app.advance_orders.config import advance_order_settings as cfg
from backend.app.advance_orders.parser import LineAnswer, parse_vendor_reply
from core.logging_setup import get_logger
from core.models import Vendor
from core.time_utils import now_ist_naive

logger = get_logger(__name__)


# ------------------------------------------------------------------ sending
def _send_text(to: str, body: str) -> str | None:
    """Send, and return the WhatsApp message id when there is one."""
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    return WhatsAppClient(whatsapp_settings).send_text_message(to, body)


def _send_template(to: str, name: str, language: str, params: list[str]) -> str | None:
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    return WhatsAppClient(whatsapp_settings).send_template_message(to, name, language, params)


def _internal_numbers(session: Session) -> list[str]:
    from backend.app.integrations.whatsapp.recipients import internal_file_recipients

    return internal_file_recipients(session)


def _vendor_numbers(vendor_id: int, session: Session) -> list[str]:
    """Every number this vendor can be asked on: his registered numbers (a
    stock-sharing vendor) plus his enquiry contacts (`advance_vendor_contacts`,
    for the vendors who never send stock). Deduplicated, and never an admin
    number -- the Founder's phone is not a vendor even if a sheet says so."""
    from backend.app.integrations.whatsapp.config import whatsapp_settings
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber

    registered = session.execute(
        select(WhatsAppRegisteredNumber.whatsapp_number).where(WhatsAppRegisteredNumber.vendor_id == vendor_id)
    ).scalars()
    contacts = session.execute(
        select(m.AdvanceVendorContact.whatsapp_number).where(
            m.AdvanceVendorContact.vendor_id == vendor_id,
            m.AdvanceVendorContact.active.is_(True),
        )
    ).scalars()
    admins = {_normalize(a) for a in whatsapp_settings.admin_phone_numbers}
    out: list[str] = []
    for raw in [*registered, *contacts]:
        number = _normalize(raw) if raw else ""
        if number and number not in admins and number not in out:
            out.append(number)
    return out


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
    """The VENDOR BRAND MAPPING's spelling ('MARUTI' -> 'MARUTI SUZUKI')."""
    from backend.app.advance_orders.brands import canonical_brand

    return canonical_brand(value) or cfg.default_brand


def _fmt_day(d: date | None) -> str:
    return d.strftime("%d %b").lstrip("0") if d else ""


def _vendor_window() -> tuple[int, int] | None:
    """(start, end) of vendor hours in minutes after midnight, or None."""
    try:
        start_text, end_text = cfg.vendor_hours.split("-")
        sh, sm = (int(x) for x in start_text.strip().split(":"))
        eh, em = (int(x) for x in end_text.strip().split(":"))
    except ValueError:
        return None
    start, end = sh * 60 + sm, eh * 60 + em
    return (start, end) if end > start else None


def _add_vendor_hours(start: datetime, minutes: int) -> datetime:
    """`start` + `minutes` counted ONLY inside vendor hours (e.g. 09:00-21:00):
    a vendor asked at 17:00 with 9 hours gets 4 that evening and 5 the next
    morning -> 14:00 next day, never 02:00 at night (Founder, 9 Oct 2026)."""
    window = _vendor_window()
    if window is None:
        return start + timedelta(minutes=minutes)
    open_at, close_at = window
    current = start
    left = minutes
    while left > 0:
        day = current.replace(hour=0, minute=0, second=0, microsecond=0)
        opens = day + timedelta(minutes=open_at)
        closes = day + timedelta(minutes=close_at)
        if current < opens:
            current = opens
        if current >= closes:
            current = opens + timedelta(days=1)
            continue
        usable = (closes - current).total_seconds() / 60
        if left <= usable:
            return current + timedelta(minutes=left)
        left -= usable
        current = opens + timedelta(days=1)
    return current


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
    """Which brand's vendors to ask about this part.

    1. the brand the sales bot sent, when it sent one;
    2. `parts.brand` from the canonical part master;
    3. `part_brand_hints`, loaded from the Founder's stock sheets;
    4. otherwise "*", the catch-all vendor list.

    The part number is NORMALISED before looking it up. `parts` stores
    `canonical_part_number` with every separator stripped (`normalise_part_number`),
    so the previous lookup -- which only upper-cased -- silently missed any
    part the sales bot wrote with a dash or a space, and routed it to "*"."""
    if given:
        return _brand(given)
    from core.ingestion.column_detector import normalise_part_number
    from core.models import Part

    key = normalise_part_number(part_number)
    if not key:
        return _brand(None)
    part = session.execute(select(Part).where(Part.canonical_part_number == key)).scalar_one_or_none()
    if part is not None and part.brand:
        return _brand(part.brand)
    hint = session.execute(select(m.PartBrandHint).where(m.PartBrandHint.part_number == key)).scalar_one_or_none()
    if hint is not None:
        return _brand(hint.brand)
    from backend.app.advance_orders.brands import brand_from_pattern

    return _brand(brand_from_pattern(key))


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
        kind=m.KIND_DEALER_STOCK if payload.get("kind") == m.KIND_DEALER_STOCK else m.KIND_ADVANCE,
    )
    if order.kind == m.KIND_DEALER_STOCK:
        order.deadline_at = now_ist_naive() + timedelta(hours=cfg.dealer_stock_window_hours)
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
                dealer_id=int(line["dealer_id"]) if line.get("dealer_id") not in (None, "") else None,
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
def _open_queries_for_brand(
    order: m.AdvanceOrder, brand: str, session: Session
) -> list[m.AdvanceVendorQuery]:
    return list(
        session.execute(
            select(m.AdvanceVendorQuery).where(
                m.AdvanceVendorQuery.advance_order_id == order.id,
                m.AdvanceVendorQuery.brand == brand,
                m.AdvanceVendorQuery.status.in_([m.Q_QUEUED, m.Q_SENT]),
            )
        ).scalars()
    )


def _asked_vendor_ids(order: m.AdvanceOrder, brand: str, session: Session) -> set[int]:
    return set(
        session.execute(
            select(m.AdvanceVendorQuery.vendor_id).where(
                m.AdvanceVendorQuery.advance_order_id == order.id,
                m.AdvanceVendorQuery.brand == brand,
            )
        ).scalars()
    )


def _resolve_lines(order: m.AdvanceOrder, session: Session, now: datetime) -> bool:
    """Decide every line whose quotes are all in, or whose window has closed.

    True when at least one line was decided -- the caller uses that to tell
    the sales bot that something changed."""
    all_queries = list(
        session.execute(
            select(m.AdvanceVendorQuery).where(m.AdvanceVendorQuery.advance_order_id == order.id)
        ).scalars()
    )
    decided = False
    for line in order.lines:
        if line.status != m.LINE_ASKING:
            continue
        covering = [q for q in all_queries if line.id in (q.line_ids or [])]
        if not covering:
            continue
        # Pending FOR THIS LINE: asked, and has not yet said anything about it.
        answered = {x.vendor_id for x in quotes.for_line(line.id, session)}
        still_open = [
            q for q in covering if q.status in (m.Q_QUEUED, m.Q_SENT) and q.vendor_id not in answered
        ]
        # Wait while a vendor we asked might still answer -- unless the
        # customer has already waited out the quote window, in which case the
        # best answer ON THE TABLE beats a longer silence.
        if still_open and not quotes.window_closed(line, covering, now):
            continue
        if quotes.apply_winner(line, session):
            decided = True
        # Nothing available yet: the line stays ASKING so the next batch of
        # vendors for this brand gets asked. It only becomes unavailable once
        # that list is exhausted -- see _advance below.
    if decided:
        session.flush()
    return decided


def _advance(order: m.AdvanceOrder, session: Session, now: datetime) -> None:
    """Decide what can be decided, then keep up to `quote_fanout` vendors in
    play for every brand that still has unanswered lines. When a brand's
    vendor list runs out, its remaining lines are marked unavailable."""
    if order.status not in (m.ASKING,):
        return
    _resolve_lines(order, session, now)

    if order.kind == m.KIND_DEALER_STOCK:
        _mark_ordered(order, session)

    # Case 3 bundles a brand's parts into one question per vendor. Case 2
    # routes each part on its own: who holds THIS part decides who is asked.
    waiting: dict[str, list[m.AdvanceOrderLine]] = {}
    for line in order.lines:
        if line.status == m.LINE_ASKING:
            key = f"#{line.id}" if order.kind == m.KIND_DEALER_STOCK else line.brand
            waiting.setdefault(key, []).append(line)

    not_found: list[m.AdvanceOrderLine] = []
    for brand, lines in waiting.items():
        open_queries = _open_queries_for_brand(order, brand, session)
        slots = cfg.quote_fanout - len(open_queries)
        if slots <= 0:
            continue
        asked = _asked_vendor_ids(order, brand, session)
        candidates = [v for v in _candidates(order, brand, lines, session) if v.id not in asked][:slots]
        if not candidates:
            if open_queries:
                continue  # somebody we already asked may still answer
            # "Nobody had it" and "nobody could be reached" are different
            # facts. Telling a customer the part is unavailable when the truth
            # is that no message got through would be a false answer.
            brand_queries = [
                q
                for q in session.execute(
                    select(m.AdvanceVendorQuery).where(
                        m.AdvanceVendorQuery.advance_order_id == order.id,
                        m.AdvanceVendorQuery.brand == brand,
                    )
                ).scalars()
            ]
            unreachable = bool(brand_queries) and all(q.status == m.Q_FAILED for q in brand_queries)
            # Founder, 10 Oct 2026: a brand nobody supplies, or vendors who
            # never answered, go to Prateek sir. Only a clear "no" from every
            # vendor asked is final.
            silent = any(q.status in (m.Q_TIMEOUT, m.Q_FAILED) for q in brand_queries)
            if order.kind == m.KIND_ADVANCE and escalation.enabled() and (not asked or silent):
                reason = (
                    f"brand {brand} is not in the vendor list" if not asked
                    else f"brand {brand} vendors did not answer"
                )
                escalation.escalate(order, lines, reason, session, now)
                continue
            for line in lines:
                line.status = m.LINE_UNAVAILABLE
                if unreachable:
                    line.note = "could not reach any vendor on WhatsApp"
                elif asked:
                    line.note = "no vendor had it"
                else:
                    line.note = "no vendor listed for this brand"
                not_found.append(line)
            if unreachable:
                _tell_internal(
                    session,
                    f"Advance order #{order.id}: brand {brand} ke kisi vendor ko message nahi gaya "
                    f"({', '.join(line.part_number for line in lines)}). Number check kijiye.",
                )
            if not asked:
                _tell_internal(
                    session,
                    f"Advance order #{order.id}: brand {brand} ka koi vendor list mein nahi hai "
                    f"({', '.join(line.part_number for line in lines)}). vendor_brands mein daaliye.",
                )
            continue
        for vendor in candidates:
            session.add(
                m.AdvanceVendorQuery(
                    advance_order_id=order.id,
                    brand=brand,
                    vendor_id=vendor.id,
                    line_ids=[line.id for line in lines],
                    status=m.Q_QUEUED,
                )
            )
    session.flush()
    if not_found:
        _tell_human(session, order, not_found)
    _send_queued(order, session, now)
    if order.kind == m.KIND_DEALER_STOCK:
        _mark_ordered(order, session)
    _settle(order)


def _candidates(order: m.AdvanceOrder, key: str, lines: list[m.AdvanceOrderLine], session: Session) -> list[Vendor]:
    """Who may be asked next for this group of lines, first to last."""
    if order.kind != m.KIND_DEALER_STOCK:
        return vendors_for_brand(key, session)
    from backend.app.advance_orders import stock_routing

    line = lines[0]
    holders = [session.get(Vendor, h.vendor_id) for h in stock_routing.stock_holders(line, session)]
    seen = {v.id for v in holders if v is not None}
    # Nobody (else) holds it: the brand's vendors, as in case 3.
    fallback = [v for v in vendors_for_brand(line.brand, session) if v.id not in seen]
    return [v for v in holders if v is not None] + fallback


def _mark_ordered(order: m.AdvanceOrder, session: Session) -> None:
    """Case 2: a vendor's yes to the ORDER makes the line ordered, and he is
    thanked with exactly what he is to send."""
    for line in order.lines:
        if line.status != m.LINE_AVAILABLE:
            continue
        line.status = m.LINE_ORDERED
        if line.vendor_id:
            from backend.app.advance_orders.stock_routing import active_import_id

            line.stock_import_id = active_import_id(line.vendor_id, session)
        qty = line.available_qty or line.qty
        body = (
            f"✅ Order pakka (Cartrends ref DS-{order.id}):\n"
            f"{line.part_number} x{qty}"
            + (f" - {_fmt_day(line.eta_date)} tak" if line.eta_date else "")
            + "\nDhanyavaad."
        )
        for number in _vendor_numbers(line.vendor_id, session) if line.vendor_id else []:
            try:
                _send_text(number, body)
            except Exception:  # noqa: BLE001
                logger.exception("Dealer-stock order %s: thank-you to %s failed.", order.id, number)
    session.flush()


def _tell_human(session: Session, order: m.AdvanceOrder, lines: list[m.AdvanceOrderLine]) -> None:
    """Every vendor said no: one message, per order, to the person who takes
    it from here (Founder, 1 Oct 2026). Part, brand and quantity on each line,
    and why. Goes to ADVANCE_ORDER_HUMAN_NUMBERS, or the admins and purchase
    team until that is set."""
    who = order.customer_name or order.customer_phone or "customer"
    rows = "\n".join(
        f"{i}. {line.part_number} ({line.brand}) x{line.qty} - {line.note or 'not found'}"
        for i, line in enumerate(lines, start=1)
    )
    body = (
        f"🔎 Advance order #{order.id} ({who}): ye part kisi vendor ke paas nahi mile:\n"
        f"{rows}\n\n"
        "Kripya dekh lijiye. Desk: Advance Orders page."
    )
    numbers = [_normalize(n) for n in cfg.human_numbers] or _internal_numbers(session)
    for number in dict.fromkeys(n for n in numbers if n):
        try:
            _send_text(number, body)
        except Exception:  # noqa: BLE001 -- one failed alert must not stop the rest
            logger.exception("Advance order: could not tell %s about unfound parts.", number)
    logger.info("Advance order %s: %d part(s) not found anywhere -- sent to %d person(s).", order.id, len(lines), len(numbers))


def _question_text(order: m.AdvanceOrder, lines: list[m.AdvanceOrderLine], want_rate: bool) -> str:
    if order.kind == m.KIND_DEALER_STOCK:
        rows = "\n".join(f"{i + 1}. {line.part_number} x{line.qty}" for i, line in enumerate(lines))
        by = f"\n{_fmt_day(order.needed_by)} tak chahiye." if order.needed_by else ""
        return (
            f"Namaste, Cartrends purchase desk se. Order (ref DS-{order.id}):\n{rows}{by}\n\n"
            'Bhej sakte hain? Reply: *ok* (bhej denge, kitne din mein), *nahi*, ya *sirf 3* (agar kam hai).'
        )
    rows = "\n".join(f"{i + 1}. {line.part_number} x{line.qty}" for i, line in enumerate(lines))
    by = f"\n{_fmt_day(order.needed_by)} tak chahiye." if order.needed_by else ""
    if want_rate:
        # No standing discount on file, so the rate is asked for -- beside
        # each part, because a loose rate across several parts cannot be read.
        ask = "Ye parts mil jayenge? Har part ka rate aur kitne din mein aa sakte hain?"
        first = lines[0].part_number if lines else "PART"
        if len(lines) > 1:
            example = f'Har part ke aage likhiye, jaise: "{first} rate 450, 3 din" ya "{first} nahi".'
        else:
            example = 'Jaise: "rate 450, 3 din" ya "nahi".'
    else:
        # The discount is already agreed -- asking about money again would only
        # invite a number we would then have to reconcile against the sheet.
        ask = "Ye parts mil jayenge? Kab tak aa sakte hain?"
        example = 'Jaise: "haan 5 din", "nahi", ya har part ke aage likh dijiye.'
    return f"Namaste, Cartrends purchase desk se.\n{ask}\n{rows}{by}\n\n{example}"


def _vendor_template_ready() -> bool:
    """Use the vendor-question template only once Meta has APPROVED it --
    it reaches vendors who have never written to the bot (the 24-hour rule).
    Until then the plain question is sent, as before."""
    if not cfg.vendor_template:
        return False
    try:
        from backend.app.integrations.whatsapp.daily_stock import _meta_templates

        status, _params = _meta_templates().get(cfg.vendor_template, ("", 0))
        return status == "APPROVED"
    except Exception:  # noqa: BLE001 -- fall back to the plain question
        logger.exception("Could not check the advance-order template status.")
        return False


def _send_queued(order: m.AdvanceOrder, session: Session, now: datetime) -> None:
    queued = session.execute(
        select(m.AdvanceVendorQuery).where(
            m.AdvanceVendorQuery.advance_order_id == order.id,
            m.AdvanceVendorQuery.status == m.Q_QUEUED,
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
        terms = quotes.brand_terms(q.vendor_id, q.brand, session)
        want_rate = quotes.wants_rate(terms)
        numbers = _vendor_numbers(q.vendor_id, session)
        delivered: list[str] = []
        sent_ids: dict[str, str] = {}
        use_template = _vendor_template_ready()
        for number in numbers:
            try:
                if use_template:
                    message_id = _send_template(
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
                    message_id = _send_text(number, _question_text(order, lines, want_rate))
                delivered.append(_normalize(number))
                if isinstance(message_id, str) and message_id:
                    sent_ids[message_id] = _normalize(number)
            except Exception:  # noqa: BLE001 -- the next number / vendor is tried
                logger.exception(
                    "Advance order %s: question to vendor %s at %s failed.",
                    order.id,
                    q.vendor_id,
                    number,
                )
        q.numbers = delivered
        q.message_ids = sent_ids or None
        if delivered:
            q.status = m.Q_SENT
            q.sent_at = now
            # Both clocks run in VENDOR HOURS only -- no vendor is timed out
            # overnight while he is asleep.
            q.deadline_at = _add_vendor_hours(now, cfg.vendor_wait_minutes)
            if order.kind == m.KIND_ADVANCE and order.deadline_at is None and cfg.advance_deadline_hours:
                # The overall "not found" clock starts with the first question.
                order.deadline_at = _add_vendor_hours(now, cfg.advance_deadline_hours * 60)
            logger.info(
                "Advance order %s: asked vendor %s about %s (%s).",
                order.id,
                q.vendor_id,
                q.brand,
                "rate + days" if want_rate else "days only",
            )
        else:
            q.status = m.Q_FAILED
            logger.warning(
                "Advance order %s: vendor %s could not be messaged (no number / send failed).",
                order.id,
                q.vendor_id,
            )
    session.flush()
    # A vendor who could not be reached is skipped straight away.
    if any(q.status == m.Q_FAILED for q in queued):
        _advance(order, session, now)


def _settle(order: m.AdvanceOrder) -> None:
    if order.status != m.ASKING:
        return
    if any(line.status in (m.LINE_ASKING, m.LINE_ESCALATED) for line in order.lines):
        return
    if order.kind == m.KIND_DEALER_STOCK:
        # The vendors have already been ordered from; nothing waits for a
        # customer's yes, so the order is done.
        ordered = any(line.status == m.LINE_ORDERED for line in order.lines)
        order.status = m.CONFIRMED if ordered else m.NO_VENDOR
        if ordered:
            order.confirmed_at = now_ist_naive()
    else:
        order.status = m.QUOTED if any(line.status == m.LINE_AVAILABLE for line in order.lines) else m.NO_VENDOR
    logger.info("Advance order %s is %s.", order.id, order.status)


def _snapshot(order: m.AdvanceOrder) -> tuple[str, tuple[tuple[int, str], ...]]:
    return order.status, tuple((line.id, line.status) for line in order.lines)


def _notify_if_changed(
    order: m.AdvanceOrder, before: tuple[str, tuple[tuple[int, str], ...]], session: Session
) -> None:
    """Push to the sales bot when a line was decided or the order settled.
    Silent otherwise -- a vendor saying "nahi" to one of three asked is not
    news the bot can act on."""
    after = _snapshot(order)
    if after == before:
        return
    event = (
        callback.EVENT_ORDER_SETTLED
        if before[0] == m.ASKING and order.status != m.ASKING
        else callback.EVENT_QUOTE_UPDATED
    )
    callback.notify(event, order_out(order, session))


def tick(session: Session, now: datetime | None = None) -> None:
    """Send what is queued (inside vendor hours), move past silent vendors,
    and decide lines whose quote window has closed."""
    now = now or now_ist_naive()
    overdue = session.execute(
        select(m.AdvanceVendorQuery).where(
            m.AdvanceVendorQuery.status == m.Q_SENT, m.AdvanceVendorQuery.deadline_at < now
        )
    ).scalars().all()
    for q in overdue:
        q.status = m.Q_TIMEOUT
        logger.info("Advance order %s: vendor %s did not answer in time.", q.advance_order_id, q.vendor_id)
    session.flush()
    for order in session.execute(select(m.AdvanceOrder).where(m.AdvanceOrder.status == m.ASKING)).scalars():
        before = _snapshot(order)
        if order.deadline_at is not None and now >= order.deadline_at:
            _expire(order, session)
        else:
            _advance(order, session, now)
        _notify_if_changed(order, before, session)


def _expire(order: m.AdvanceOrder, session: Session) -> None:
    """Case 2's window is over: what is still open is not found -- told to a
    person -- and what was ordered stands.

    Advance orders: parts still waiting on vendors go to Prateek sir first
    (escalation.py); parts already with him and unanswered are not found."""
    if order.kind == m.KIND_ADVANCE and escalation.enabled():
        asking = [line for line in order.lines if line.status == m.LINE_ASKING]
        if asking:
            for q in session.execute(
                select(m.AdvanceVendorQuery).where(
                    m.AdvanceVendorQuery.advance_order_id == order.id,
                    m.AdvanceVendorQuery.status.in_([m.Q_QUEUED, m.Q_SENT]),
                )
            ).scalars():
                q.status = m.Q_TIMEOUT
            escalation.escalate(
                order, asking, f"no vendor answered within {cfg.advance_deadline_hours} working hours",
                session, now_ist_naive(),
            )
            return
        for line in order.lines:
            if line.status == m.LINE_ESCALATED:
                line.status = m.LINE_UNAVAILABLE
                line.note = f"no answer from Prateek sir within {cfg.escalation_hours} h"
        session.flush()
        _settle(order)
        return
    open_lines = [line for line in order.lines if line.status == m.LINE_ASKING]
    for line in open_lines:
        line.status = m.LINE_UNAVAILABLE
        hours = cfg.dealer_stock_window_hours if order.kind == m.KIND_DEALER_STOCK else cfg.advance_deadline_hours
        line.note = f"not found within {hours} h"
    for q in session.execute(
        select(m.AdvanceVendorQuery).where(
            m.AdvanceVendorQuery.advance_order_id == order.id,
            m.AdvanceVendorQuery.status.in_([m.Q_QUEUED, m.Q_SENT]),
        )
    ).scalars():
        q.status = m.Q_CLOSED
    session.flush()
    if open_lines:
        _tell_human(session, order, open_lines)
    _settle(order)


def handle_delivery_failure(message_id: str, recipient: str, error_code: int | None, error_title: str | None, session: Session) -> bool:
    """WhatsApp reported that a question to a vendor was NOT delivered.

    Most often 131047: he has not messaged the bot in 24 hours, so only an
    approved template reaches him (ADVANCE_ORDER_VENDOR_TEMPLATE). Without
    this, the question counted as sent and the order waited the full
    VENDOR_WAIT_MINUTES for a reply that could never come. Now that number
    is dropped at once; when none of the vendor's numbers got it, he is
    skipped and the NEXT vendor is asked straight away. True when the report
    belonged to an advance-order question."""
    number = _normalize(recipient) if recipient else ""
    for q in session.execute(select(m.AdvanceVendorQuery).where(m.AdvanceVendorQuery.status == m.Q_SENT)).scalars():
        ids = q.message_ids or {}
        if message_id not in ids:
            continue
        failed_number = ids.get(message_id) or number
        q.message_ids = {k: v for k, v in ids.items() if k != message_id} or None
        q.numbers = [n for n in (q.numbers or []) if n != failed_number]
        why = f"WhatsApp did not deliver ({error_code or '?'}: {error_title or 'no reason'})"
        if not q.numbers:
            q.status = m.Q_FAILED
            q.reply_text = why
        logger.warning("Advance order %s: question to vendor %s at %s not delivered -- %s.", q.advance_order_id, q.vendor_id, failed_number, why)
        session.flush()
        order = session.get(m.AdvanceOrder, q.advance_order_id)
        if order is not None and q.status == m.Q_FAILED:
            before = _snapshot(order)
            _advance(order, session, now_ist_naive())
            _notify_if_changed(order, before, session)
        return True
    return False


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
    mine: list[m.AdvanceVendorQuery] = []
    for x in open_queries:
        if number not in (x.numbers or []):
            continue
        # A question whose order has moved on, or whose lines have all been
        # decided, can no longer be answered. It is closed here rather than
        # left to catch -- and swallow -- this vendor's reply to a LIVE one.
        live_order = session.get(m.AdvanceOrder, x.advance_order_id)
        live = (
            live_order is not None
            and live_order.status == m.ASKING
            and any(line.id in (x.line_ids or []) and line.status == m.LINE_ASKING for line in live_order.lines)
        )
        if live:
            mine.append(x)
        else:
            x.status = m.Q_CLOSED
    if not mine:
        return False
    # ONE NUMBER, SEVERAL VENDORS. The Founder's sheet has numbers shared by
    # two or three vendor names (919831799000 is listed for Anv Marketing,
    # Mahesh Motors and Wow Trexim). If more than one of them has an open
    # question, a reply from that number cannot be pinned to one vendor --
    # taking the latest would file one vendor's price under another's name.
    # So it is not guessed: the admin sees the text and records it by hand.
    vendor_ids = {x.vendor_id for x in mine}
    if len(vendor_ids) > 1:
        names = [
            (session.get(Vendor, vid).name if session.get(Vendor, vid) else str(vid))
            for vid in sorted(vendor_ids)
        ]
        _tell_internal(
            session,
            f"Advance order: {number} ek hi number hai {len(names)} vendors ka "
            f"({', '.join(names)}), aur sabse sawaal khula hai. Ye jawab kiska hai, "
            f"pata nahi chal sakta:\n\"{text[:300]}\"\n"
            "Desk pe sahi vendor ke naam se daal dijiye (Advance Orders page).",
        )
        return True
    q = mine[0]  # the latest question -- the original rule
    if len(mine) > 1:
        # Several live enquiries to this one vendor. If the reply names part
        # numbers belonging to exactly one of them, that is the one he means.
        import re as _re

        said = {_re.sub(r"[^A-Z0-9]", "", tok.upper()) for tok in _re.findall(r"[A-Za-z0-9-]{5,}", text or "")}
        named = []
        for x in mine:
            o = session.get(m.AdvanceOrder, x.advance_order_id)
            parts = {_re.sub(r"[^A-Z0-9]", "", line.part_number.upper()) for line in o.lines if line.id in (x.line_ids or [])}
            if parts & said:
                named.append(x)
        if len(named) == 1:
            q = named[0]
    order = session.get(m.AdvanceOrder, q.advance_order_id)
    if order is None or order.status != m.ASKING:
        q.status = m.Q_CLOSED
        return False
    by_id = {line.id: line for line in order.lines}
    lines = [by_id[i] for i in q.line_ids if i in by_id and by_id[i].status == m.LINE_ASKING]
    terms = quotes.brand_terms(q.vendor_id, q.brand, session)
    want_rate = quotes.wants_rate(terms)
    answers = parse_vendor_reply(
        text, [(line.id, line.part_number, line.qty) for line in lines], now.date(), want_rate
    )
    # The fixed rules could not read it all -- ask the AI, whose answer is
    # kept only where every number is in the vendor's own words (ai_reply).
    # A part the fixed rules read cleanly keeps the fixed rules' reading.
    #
    # Also when the reply carries a number the fixed rules did not use: "stock
    # me 3 hi bache hain" reads to them as "available" (all 10), the 3 left
    # unread. Then the AI's reading -- still checked against his words -- wins.
    from backend.app.advance_orders.parser import _residual_numbers

    unread_numbers = bool(answers) and bool(_residual_numbers(text))
    if not answers or any(a.ambiguous for a in answers.values()) or len(answers) < len(lines) or unread_numbers:
        from backend.app.advance_orders import ai_reply

        ai_answers = ai_reply.read(
            text, [(line.id, line.part_number, line.qty) for line in lines], now.date(), want_rate
        )
        if ai_answers:
            merged = {k: v for k, v in (answers or {}).items() if not v.ambiguous}
            for line_id, answer in ai_answers.items():
                if unread_numbers:
                    merged[line_id] = answer
                else:
                    merged.setdefault(line_id, answer)
            answers = merged

    vendor = session.get(Vendor, q.vendor_id)
    vendor_label = vendor.name if vendor else "vendor"
    if not answers:
        _tell_internal(
            session,
            f"Advance order #{order.id}: {vendor_label} ka jawab samajh nahi aaya:\n\"{text[:300]}\"\n"
            f"Parts: {', '.join(line.part_number + ' x' + str(line.qty) for line in lines)}",
        )
        return True

    # A number that might be the rate, but was not labelled as one. Guessing
    # it would put an invented price in front of a customer, so those lines
    # are NOT recorded; the admin reads the vendor's own words instead.
    ambiguous = [line for line in lines if answers.get(line.id) is not None and answers[line.id].ambiguous]
    clean = [line for line in lines if answers.get(line.id) is not None and not answers[line.id].ambiguous]
    if ambiguous:
        _tell_internal(
            session,
            f"Advance order #{order.id}: {vendor_label} ne rate saaf nahi likha:\n\"{text[:300]}\"\n"
            f"Parts: {', '.join(line.part_number + ' x' + str(line.qty) for line in ambiguous)}\n"
            "Desk pe haath se daal dijiye (Advance Orders page).",
        )
    if not clean:
        # Nothing usable arrived. The question stays open, so a clarifying
        # reply from the vendor is still read, and the desk can type it in.
        return True

    q.status = m.Q_REPLIED
    q.replied_at = now
    q.reply_text = text[:1000]
    for line in clean:
        quotes.record(
            order_id=order.id,
            line=line,
            vendor_id=q.vendor_id,
            answer=answers[line.id],
            terms=terms,
            session=session,
            query_id=q.id,
            raw_reply=text,
        )
    session.flush()
    before = _snapshot(order)
    _advance(order, session, now)
    _notify_if_changed(order, before, session)
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
        # Dealer Portal: the purchase order and its transit entry.
        from backend.app.advance_orders import dealer_portal_po

        portal = dealer_portal_po.create_po_and_transit(order, vendor, lines)
        refs = dict(order.dealer_portal or {})
        refs[str(vendor_id)] = portal
        order.dealer_portal = refs
        portal_line = (
            f"Dealer Portal: PO {portal['po']} · transit {portal['transit']}"
            if not portal.get("error")
            else f"Dealer Portal: FAILED ({portal['error']})"
        )
        summary.append(f"{vendor.name if vendor else vendor_id}:\n{rows}\n{portal_line}")
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
    tat_days: int | None = None,
    quoted_rate: Decimal | None = None,
    mrp: Decimal | None = None,
    force: bool = True,
) -> m.AdvanceOrder:
    """An admin who got the answer on the phone records it here.

    `force=True` (the default, and the original behaviour) makes this vendor
    the answer for the line outright -- the admin has decided. `force=False`
    stores it as one more quote and lets the ranking choose, which is what you
    want when typing in a reply the parser refused as ambiguous."""
    line = next((x for x in order.lines if x.id == line_id), None)
    if line is None:
        raise ValueError("no such line")
    if order.status != m.ASKING:
        raise ValueError(f"order is {order.status}")
    before = _snapshot(order)

    if tat_days is None and eta is not None:
        tat_days = max(0, (eta - now_ist_naive().date()).days)
    if eta is None and tat_days is not None:
        eta = now_ist_naive().date() + timedelta(days=tat_days)

    if vendor_id is not None:
        quotes.record(
            order_id=order.id,
            line=line,
            vendor_id=vendor_id,
            answer=LineAnswer(
                available=available,
                available_qty=available_qty,
                eta=eta,
                tat_days=tat_days,
                quoted_rate=quoted_rate,
                mrp=mrp,
            ),
            terms=quotes.brand_terms(vendor_id, line.brand, session),
            session=session,
            raw_reply=None,
            source="admin",
        )

    if not force and vendor_id is not None:
        # One more quote on the table -- let the ranking decide, exactly as it
        # would for a WhatsApp reply. If nobody has it yet the line stays
        # open, because other vendors asked may still say yes.
        quotes.apply_winner(line, session)
        session.flush()
        _settle(order)
        _notify_if_changed(order, before, session)
        return order

    line.status = m.LINE_AVAILABLE if available else m.LINE_UNAVAILABLE
    line.vendor_id = vendor_id if available else None
    line.available_qty = (min(available_qty, line.qty) if available_qty else line.qty) if available else None
    line.eta_date = eta if available else None
    line.tat_days = tat_days if available else None
    if available and vendor_id is not None:
        chosen = next(
            (x for x in quotes.for_line(line.id, session) if x.vendor_id == vendor_id), None
        )
        if chosen is not None:
            line.mrp = chosen.mrp
            line.discount_pct = chosen.discount_pct
            line.net_price = chosen.net_price
            line.winning_quote_id = chosen.id
    line.note = "entered by admin"
    session.flush()
    _settle(order)
    _notify_if_changed(order, before, session)
    return order


def _money(value: Decimal | None) -> str | None:
    """Decimals travel as strings: JSON floats would round a rupee figure."""
    return ranking.fmt_money(value)


def _quote_out(quote: m.AdvanceVendorQuote, vendors: dict[int, str], brand: str, session: Session) -> dict:
    terms = quotes.brand_terms(quote.vendor_id, brand, session)
    band = ranking.tat_band(quote.tat_days)
    return {
        "id": quote.id,
        "vendor_id": quote.vendor_id,
        "vendor_name": vendors.get(quote.vendor_id),
        "available": quote.available,
        "available_qty": quote.available_qty,
        "tat_days": quote.tat_days,
        "tat_band": ranking.band_label(band),
        "eta_date": quote.eta_date.isoformat() if quote.eta_date else None,
        "mrp": _money(quote.mrp),
        "quoted_rate": _money(quote.quoted_rate),
        "discount_pct": _money(quote.discount_pct),
        "net_price": _money(quote.net_price),
        "price_source": quote.price_source,
        "source": quote.source,
        # The vendor's standing terms, so the desk can judge a quote the
        # ranking does not score (payment terms, pickup).
        "terms": {
            "discount_type": terms.discount_type if terms else None,
            "discount_note": terms.discount_note if terms else None,
            "transport": terms.transport if terms else None,
            "payment_terms": terms.payment_terms if terms else None,
        },
        "raw_reply": quote.raw_reply,
        "received_at": quote.created_at.isoformat() if quote.created_at else None,
    }


def order_out(order: m.AdvanceOrder, session: Session) -> dict:
    """What the sales bot reads. Vendor names are included for the desk; the
    sales bot never shows them to a customer.

    Every key this returned before quotes existed is still here with the same
    meaning, so a sales bot written against the old shape keeps working. The
    new keys are additions only: the winning quote's figures on each line,
    `best_quote`, and `quotes` -- every vendor's answer, best first."""
    vendors = {v.id: v.name for v in session.execute(select(Vendor)).scalars()} if order.lines else {}
    lines_out = []
    for line in order.lines:
        ranked = quotes.ranked_for_line(line, session)
        ranked_ids = [r.quote.id for r in ranked]
        all_quotes = quotes.for_line(line.id, session)
        # Available quotes in rank order, then the refusals, newest first.
        ordered = [r.quote for r in ranked] + sorted(
            (x for x in all_quotes if x.id not in ranked_ids),
            key=lambda x: x.created_at or datetime.min,
            reverse=True,
        )
        winner = next((x for x in all_quotes if x.id == line.winning_quote_id), None)
        lines_out.append(
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
                # --- added with quote comparison -----------------------------
                "vendor_id": line.vendor_id,
                "dealer_id": line.dealer_id,
                "tat_days": line.tat_days,
                "tat_band": ranking.band_label(ranking.tat_band(line.tat_days)) if line.status == m.LINE_AVAILABLE else None,
                "mrp": _money(line.mrp),
                "discount_pct": _money(line.discount_pct),
                "net_price": _money(line.net_price),
                "best_quote": _quote_out(winner, vendors, line.brand, session) if winner else None,
                "quote_count": len(all_quotes),
                "quotes": [_quote_out(x, vendors, line.brand, session) for x in ordered],
            }
        )
    return {
        "id": order.id,
        "external_ref": order.external_ref,
        "status": order.status,
        "dealer_portal": order.dealer_portal or {},
        "customer": {"portal_id": order.customer_portal_id, "name": order.customer_name, "phone": order.customer_phone},
        "needed_by": order.needed_by.isoformat() if order.needed_by else None,
        "confirmed_at": order.confirmed_at.isoformat() if order.confirmed_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "kind": order.kind or m.KIND_ADVANCE,
        "deadline_at": order.deadline_at.isoformat() if order.deadline_at else None,
        "lines": lines_out,
    }
