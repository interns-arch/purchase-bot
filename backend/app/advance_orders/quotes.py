"""Recording what each vendor said, and deciding when a line has an answer.

`service.py` runs the conversation; this module owns the bookkeeping around
it, so neither file has to hold both.

WHY A LINE IS NOT DECIDED BY THE FIRST "HAAN"
---------------------------------------------
The original flow took the first vendor who said yes and stopped. That is the
right answer when the only question is "can anyone supply this", and the
wrong one the moment terms matter: the second vendor's better discount is
never seen, because he is never asked.

So a line now collects quotes and is decided when EITHER every vendor asked
has answered, OR the quote window has run out -- whichever comes first. A
customer is never left waiting on a silent vendor, and a vendor who answers
promptly is never thrown away because a slower one might have been cheaper.

If nobody asked so far has it, the line stays open and `service._advance()`
asks the next batch of vendors for that brand. Only when the brand's vendor
list is exhausted does the line become unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from backend.app.advance_orders import ranking
from backend.app.advance_orders.config import advance_order_settings as cfg
from backend.app.advance_orders.parser import LineAnswer
from core.logging_setup import get_logger

logger = get_logger(__name__)


# ------------------------------------------------------------------ terms
def brand_terms(vendor_id: int, brand: str, session: Session) -> m.VendorBrand | None:
    """This vendor's standing arrangement for this brand, falling back to the
    catch-all list exactly as vendor selection does."""
    row = session.execute(
        select(m.VendorBrand).where(
            m.VendorBrand.vendor_id == vendor_id, m.VendorBrand.brand == brand
        )
    ).scalar_one_or_none()
    if row is None and brand != cfg.default_brand:
        row = session.execute(
            select(m.VendorBrand).where(
                m.VendorBrand.vendor_id == vendor_id,
                m.VendorBrand.brand == cfg.default_brand,
            )
        ).scalar_one_or_none()
    return row


def wants_rate(terms: m.VendorBrand | None) -> bool:
    """True when this vendor must be ASKED for a price.

    Only a vendor the mapping EXPLICITLY marks as rate-based -- "rate",
    "rate + scheme", "Net Rate", "1000 discount" -- is asked for a number. A
    vendor with a standing percentage already has a known discount, so his
    question stays "do you have it, and in how many days".

    A vendor-brand row with NO terms on file at all (made before the mapping
    was imported, or by hand on the desk) keeps the original question and the
    original one-message-for-all-parts shape. Treating "nothing on file" as
    "rate vendor" would quietly change how every pre-existing vendor is
    messaged -- split into one message per part, asked about money -- and
    that is a change nobody asked for. Such a quote is simply unpriced
    (`price_source = "none"`), which the desk can see."""
    if terms is None:
        return False
    return terms.discount_type in (m.DISC_RATE, m.DISC_OTHER)


# ------------------------------------------------------------------ writing
def record(
    *,
    order_id: int,
    line: m.AdvanceOrderLine,
    vendor_id: int,
    answer: LineAnswer,
    terms: m.VendorBrand | None,
    session: Session,
    query_id: int | None = None,
    raw_reply: str | None = None,
    source: str = "whatsapp",
) -> m.AdvanceVendorQuote:
    """Store one vendor's answer about one line, with the money worked out.

    Re-answering replaces that vendor's own previous quote (a vendor who
    corrects himself is believed), but never touches another vendor's."""
    standing_pct: Decimal | None = None
    discount_type = m.DISC_RATE
    if terms is not None:
        discount_type = terms.discount_type
        standing_pct = terms.discount_pct

    net = ranking.net_unit_price(
        discount_type=discount_type,
        standing_pct=standing_pct,
        quoted_rate=answer.quoted_rate,
        mrp=answer.mrp,
    )
    effective = ranking.effective_discount_pct(
        discount_type=discount_type,
        standing_pct=standing_pct,
        quoted_rate=answer.quoted_rate,
        mrp=answer.mrp,
    )
    if answer.quoted_rate is not None:
        price_source = "quoted"
    elif net is not None:
        price_source = "standing"
    else:
        price_source = "none"

    quote = session.execute(
        select(m.AdvanceVendorQuote).where(
            m.AdvanceVendorQuote.advance_order_line_id == line.id,
            m.AdvanceVendorQuote.vendor_id == vendor_id,
        )
    ).scalar_one_or_none()
    if quote is None:
        quote = m.AdvanceVendorQuote(
            advance_order_id=order_id,
            advance_order_line_id=line.id,
            vendor_id=vendor_id,
        )
        session.add(quote)

    quote.query_id = query_id
    quote.available = bool(answer.available)
    quote.available_qty = min(answer.available_qty, line.qty) if answer.available_qty else None
    quote.tat_days = answer.tat_days
    quote.eta_date = answer.eta
    quote.mrp = answer.mrp
    quote.quoted_rate = answer.quoted_rate
    quote.discount_pct = effective
    quote.net_price = net
    quote.price_source = price_source
    quote.raw_reply = (raw_reply or "")[:1000] or None
    quote.source = source
    session.flush()
    return quote


def for_line(line_id: int, session: Session) -> list[m.AdvanceVendorQuote]:
    return list(
        session.execute(
            select(m.AdvanceVendorQuote).where(
                m.AdvanceVendorQuote.advance_order_line_id == line_id
            )
        ).scalars()
    )


# ------------------------------------------------------------------ deciding
def _scored(
    quotes: list[m.AdvanceVendorQuote], brand: str, session: Session
) -> list[ranking.ScoredQuote]:
    out: list[ranking.ScoredQuote] = []
    for quote in quotes:
        terms = brand_terms(quote.vendor_id, brand, session)
        out.append(
            ranking.ScoredQuote(
                quote=quote,
                band=ranking.tat_band(quote.tat_days),
                discount_pct=quote.discount_pct,
                net_price=quote.net_price,
                vendor_priority=terms.priority if terms is not None else 1000,
            )
        )
    return out


def ranked_for_line(line: m.AdvanceOrderLine, session: Session) -> list[ranking.ScoredQuote]:
    """Every available quote for this line, best first."""
    return ranking.rank(_scored(for_line(line.id, session), line.brand, session))


def window_closed(
    line: m.AdvanceOrderLine, queries: list[m.AdvanceVendorQuery], now: datetime
) -> bool:
    """Has this line waited long enough to be decided on what has arrived?

    Timed from the FIRST question that went out about it, so adding a second
    vendor later does not restart the customer's wait."""
    sent = [q.sent_at for q in queries if q.sent_at is not None]
    if not sent:
        return False
    return min(sent) + timedelta(minutes=cfg.quote_window_minutes) <= now


def apply_winner(line: m.AdvanceOrderLine, session: Session, _depth: int = 0) -> bool:
    """Copy the best available quote onto the line. True when one existed.

    A vendor who has only part of the line ("sirf 3" of 10) gets HIS part:
    the line becomes 3, and a new line for the other 7 is opened for the next
    vendor (`ADVANCE_ORDER_SPLIT_PARTIAL`). Any other vendor who already said
    yes to the line is carried over to the remainder, so he is not asked
    twice. The quantity the sales bot asked for, added up, never changes."""
    ordered = ranked_for_line(line, session)
    if not ordered:
        return False
    winner = ordered[0]
    quote = winner.quote
    line.status = m.LINE_AVAILABLE
    line.vendor_id = quote.vendor_id
    line.available_qty = quote.available_qty or line.qty
    line.eta_date = quote.eta_date
    line.tat_days = quote.tat_days
    line.mrp = quote.mrp
    line.discount_pct = quote.discount_pct
    line.net_price = quote.net_price
    line.winning_quote_id = quote.id
    line.note = ranking.explain(winner, ordered)
    logger.info(
        "Advance line %s decided: vendor %s (%s).", line.id, quote.vendor_id, line.note
    )

    have = quote.available_qty
    if cfg.split_partial and have and 0 < have < line.qty and _depth < 25:
        remainder = line.qty - have
        line.qty = have
        line.available_qty = have
        line.note = f"{line.note} - had {have} of {have + remainder}"
        rest = m.AdvanceOrderLine(
            part_number=line.part_number,
            part_name=line.part_name,
            brand=line.brand,
            qty=remainder,
            status=m.LINE_ASKING,
            note=f"rest of {line.part_number}: {remainder} still to find",
        )
        line.order.lines.append(rest)
        session.flush()
        carried = False
        for other in ordered[1:]:
            source = other.quote
            session.add(
                m.AdvanceVendorQuote(
                    advance_order_id=line.advance_order_id,
                    advance_order_line_id=rest.id,
                    vendor_id=source.vendor_id,
                    query_id=source.query_id,
                    available=True,
                    available_qty=min(source.available_qty or remainder, remainder),
                    tat_days=source.tat_days,
                    eta_date=source.eta_date,
                    mrp=source.mrp,
                    quoted_rate=source.quoted_rate,
                    discount_pct=source.discount_pct,
                    net_price=source.net_price,
                    price_source=source.price_source,
                    raw_reply=source.raw_reply,
                    source=source.source,
                )
            )
            carried = True
        session.flush()
        logger.info("Advance line %s split: %s from vendor %s, %s still to find.", line.id, have, quote.vendor_id, remainder)
        if carried:
            apply_winner(rest, session, _depth + 1)
    return True
