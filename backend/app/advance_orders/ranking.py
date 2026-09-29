"""Choosing between vendors who all said yes.

THE FOUNDER'S RULE (recorded 29 Sep 2026)
-----------------------------------------
    "1st better discount or better tat, 2 better tat less discount,
     best discount worst tat"

Read as an ordering of three outcomes: best is good on both; a better TAT
with a smaller discount beats a bigger discount that arrives late; the
biggest discount with the worst TAT is the last thing to pick.

So TAT outranks discount -- but only ACROSS A BAND. Comparing raw days would
let "one day sooner, nine percent worse" win, which is not what that rule
means. Quotes are bucketed by TAT first (`ADVANCE_ORDER_TAT_BANDS`, days:
1,3,7,15 by default), and inside one bucket the two vendors are treated as
equally quick, so the better commercial terms decide.

WHAT COUNTS AS "BETTER TERMS"
-----------------------------
Two different vendors give you two different kinds of number:

  * a PERCENT vendor has a standing discount off MRP -- a percentage, and
    usually no absolute price, because he never states MRP in a WhatsApp reply;
  * a RATE vendor states an absolute rate per part -- a price, and no
    percentage, unless he also happened to mention MRP.

They are only truly comparable when MRP is known for both, which is rare. So
the comparison is layered rather than forced: quotes carrying an effective
discount percentage are compared with each other, quotes carrying only a net
price are compared with each other, and a quote carrying neither is ranked
last. Nothing is converted using an assumed MRP -- an invented MRP would
produce an invented saving, and the Command Centre's price rules already
refuse to show a figure whose basis is unknown.

A vendor who is available but quoted nothing still WINS over a vendor who
has nothing, because having the part at all is worth more than a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.app.advance_orders import models as m
from backend.app.advance_orders.config import advance_order_settings as cfg

# A TAT we were never told. Sorted into the slowest band rather than dropped:
# the vendor does have the part, he just did not say when.
UNKNOWN_TAT_BAND = 999


def fmt_money(value: Decimal | None) -> str | None:
    """A Decimal as plain digits: 380, 450.5, 21. `Decimal.normalize()` alone
    turns 380 into "3.8E+2", which is not something to send a sales bot."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text


def tat_band(tat_days: int | None) -> int:
    """Which speed bucket a promise falls in. Lower is faster."""
    if tat_days is None:
        return UNKNOWN_TAT_BAND
    for index, upper in enumerate(cfg.tat_bands):
        if tat_days <= upper:
            return index
    return len(cfg.tat_bands)


def band_label(band: int) -> str:
    """Human wording for the desk, derived from the configured bands."""
    if band == UNKNOWN_TAT_BAND:
        return "no date given"
    bands = cfg.tat_bands
    if band == 0:
        return f"within {bands[0]} day" if bands[0] == 1 else f"within {bands[0]} days"
    if band >= len(bands):
        return f"over {bands[-1]} days"
    return f"{bands[band - 1] + 1}-{bands[band]} days"


def effective_discount_pct(
    *,
    discount_type: str,
    standing_pct: Decimal | None,
    quoted_rate: Decimal | None,
    mrp: Decimal | None,
) -> Decimal | None:
    """The discount this quote really represents, or None when it cannot be
    known. Never guessed: no MRP means no percentage for a rate vendor."""
    if discount_type == m.DISC_PERCENT and standing_pct is not None:
        return standing_pct
    if quoted_rate is not None and mrp is not None and mrp > 0 and quoted_rate <= mrp:
        return ((mrp - quoted_rate) / mrp * Decimal(100)).quantize(Decimal("0.001"))
    return None


def net_unit_price(
    *,
    discount_type: str,
    standing_pct: Decimal | None,
    quoted_rate: Decimal | None,
    mrp: Decimal | None,
) -> Decimal | None:
    """What one unit actually costs, or None. A standing percentage alone is
    not a price -- it needs an MRP to become one."""
    if quoted_rate is not None:
        return quoted_rate
    if discount_type == m.DISC_PERCENT and standing_pct is not None and mrp is not None and mrp > 0:
        return (mrp * (Decimal(100) - standing_pct) / Decimal(100)).quantize(Decimal("0.0001"))
    return None


@dataclass
class ScoredQuote:
    quote: m.AdvanceVendorQuote
    band: int
    discount_pct: Decimal | None
    net_price: Decimal | None
    vendor_priority: int

    @property
    def terms_tier(self) -> int:
        """0 = comparable by discount, 1 = comparable by price only,
        2 = no commercial figure at all."""
        if self.discount_pct is not None:
            return 0
        if self.net_price is not None:
            return 1
        return 2

    def sort_key(self) -> tuple:
        # Speed band first (the Founder's rule), then which kind of figure we
        # have, then the figure itself, then the configured vendor order so
        # the outcome is stable rather than arbitrary.
        return (
            self.band,
            self.terms_tier,
            -(self.discount_pct or Decimal(0)),
            self.net_price if self.net_price is not None else Decimal("Infinity"),
            self.vendor_priority,
            self.quote.id or 0,
        )


def rank(scored: list[ScoredQuote]) -> list[ScoredQuote]:
    """Best first. Only available quotes belong here."""
    return sorted([s for s in scored if s.quote.available], key=lambda s: s.sort_key())


def best(scored: list[ScoredQuote]) -> ScoredQuote | None:
    ordered = rank(scored)
    return ordered[0] if ordered else None


def explain(winner: ScoredQuote, runners: list[ScoredQuote]) -> str:
    """One line saying why this vendor won -- shown on the desk and written
    into the line's note, so a purchase decision is never unexplained."""
    parts = [band_label(winner.band)]
    if winner.discount_pct is not None:
        parts.append(f"{fmt_money(winner.discount_pct)}% off")
    elif winner.net_price is not None:
        parts.append(f"Rs {fmt_money(winner.net_price)}/unit")
    else:
        parts.append("no price given")
    beaten = [r for r in runners if r.quote.id != winner.quote.id]
    if beaten:
        parts.append(f"best of {len(beaten) + 1} quotes")
    return " - ".join(parts)
