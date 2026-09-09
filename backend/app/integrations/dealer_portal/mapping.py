"""Ask the admin ONCE which Dealer Portal dealer a vendor belongs to, then
never ask again.

Founder, 4 Sep 2026. Several DP accounts routinely share a vendor's name --
"A K Motors Fbd" (113), "A K Motors (Debtors)" (1366), "A K Motors Karol
Bagh" (1367). Guessing would file one vendor's stock under another dealer's
id, and nobody would notice until the accounts team could not reconcile.

So when the mapping is unknown the push PAUSES and the admin gets:

    ❓ Northend Distributors ka stock kahan push karun?

    1. [29] Northend Distributors        (bulk_dealer)
    2. [1366] A K Motors (Debtors)       (bulk_dealer)

    Reply "dp 1" to pick, or "dp skip" to never push this vendor.

Their answer is stored in `DealerPortalVendorMap` and the question is never
repeated for that vendor. The IMPORT is never affected -- it has already
committed; only the push waits.

TWO REFUSALS THAT NEED NO HUMAN
-------------------------------
* A `cartrend_dealer` candidate is a CarTrends WAREHOUSE (Bijwasan,
  Maansarovar, Karol Bagh, Noida, Gurugram, Seahawk). That stock is exported
  OUT of DP, so pushing it back would double it -- those are shown greyed and
  cannot be chosen.
* If the vendor itself is own stock, the question is never asked at all; the
  mapping is recorded SKIPPED immediately.
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.integrations.dealer_portal import directory
from core.logging_setup import get_logger
from core.models import (
    DealerPortalVendorMap,
    DealerPortalVendorMapStatus,
    Vendor,
)
from core.time_utils import now_ist_naive

logger = get_logger(__name__)


def get_map(vendor_id: int, session) -> DealerPortalVendorMap | None:
    return session.execute(
        select(DealerPortalVendorMap).where(
            DealerPortalVendorMap.vendor_id == vendor_id
        )
    ).scalar_one_or_none()


def is_resolved(row: DealerPortalVendorMap | None) -> bool:
    """Whether the push may proceed without asking anyone."""
    return (
        row is not None
        and row.status == DealerPortalVendorMapStatus.CONFIRMED
        and row.dp_dealer_id is not None
    )


def is_refused(row: DealerPortalVendorMap | None) -> bool:
    """Whether the admin has said this vendor is never pushed."""
    return row is not None and row.status == DealerPortalVendorMapStatus.SKIPPED


def record_skip(vendor_id: int, vendor_name: str, session, *, reason: str) -> None:
    """Mark a vendor as never-push without asking -- used for own stock."""
    row = get_map(vendor_id, session)
    if row is None:
        row = DealerPortalVendorMap(vendor_id=vendor_id, vendor_name=vendor_name)
        session.add(row)
    row.vendor_name = vendor_name
    row.status = DealerPortalVendorMapStatus.SKIPPED
    row.confirmed_at = now_ist_naive()
    row.confirmed_by = reason
    session.flush()
    logger.info(
        "Dealer Portal: vendor %s (%s) marked never-push -- %s.",
        vendor_id,
        vendor_name,
        reason,
    )


def prepare_question(
    vendor_id: int, vendor_name: str, account, session
) -> tuple[DealerPortalVendorMap, str] | None:
    """Build the admin's question and remember the candidates offered.

    Returns (map_row, message_text), or None when the roster could not be
    read -- the caller then simply retries on the vendor's next upload rather
    than sending a question with nothing in it."""
    records = directory.load(account)
    if not records:
        logger.warning(
            "Dealer Portal: no dealer roster available, so vendor %s (%s) cannot "
            "be mapped yet. Will retry on the next upload.",
            vendor_id,
            vendor_name,
        )
        return None

    candidates = directory.search(vendor_name, records)
    row = get_map(vendor_id, session)
    if row is None:
        row = DealerPortalVendorMap(vendor_id=vendor_id, vendor_name=vendor_name)
        session.add(row)
    row.vendor_name = vendor_name
    row.status = DealerPortalVendorMapStatus.PENDING
    row.candidates = [
        {
            "dealer_id": c.dealer_id,
            "name": c.name,
            "type": c.dealer_type,
            "own_warehouse": c.is_own_warehouse,
        }
        for c in candidates
    ]
    row.asked_at = now_ist_naive()
    session.flush()

    lines = [
        f"❓ Dealer Portal mapping needed",
        "",
        f"Vendor: {vendor_name}",
        "Iska stock kis Dealer Portal account mein push karun?",
        "",
    ]
    if candidates:
        for index, c in enumerate(candidates, start=1):
            tag = "  ⛔ CarTrends warehouse" if c.is_own_warehouse else ""
            lines.append(f"{index}. [{c.dealer_id}] {c.name}")
            lines.append(f"     type: {c.dealer_type or '-'}{tag}")
        lines.append("")
        lines.append('Reply "dp 1" (ya jo number sahi ho).')
    else:
        lines.append("DP par is naam se koi dealer nahi mila.")
        lines.append("")
    lines.append('Ya seedha id: "dp 1367"')
    lines.append('Ya kabhi push mat karo: "dp skip"')
    lines.append("")
    lines.append("Ye sirf ek baar poocha jayega — jawab yaad rakha jayega.")
    return row, "\n".join(lines)


def resolve_reply(text: str, session) -> tuple[bool, str]:
    """Apply an admin's "dp ..." answer to the OLDEST pending question.

    Returns (handled, reply_text). `handled=False` means the message was not
    a dp answer at all and the caller should carry on with its normal
    handling."""
    body = (text or "").strip()
    lowered = body.lower()
    if not lowered.startswith("dp"):
        return False, ""
    argument = body[2:].strip(" :,-").strip()
    if not argument:
        return False, ""

    row = session.execute(
        select(DealerPortalVendorMap)
        .where(DealerPortalVendorMap.status == DealerPortalVendorMapStatus.PENDING)
        .order_by(DealerPortalVendorMap.asked_at, DealerPortalVendorMap.id)
    ).scalars().first()
    if row is None:
        return True, "Koi Dealer Portal mapping pending nahi hai."

    candidates = row.candidates or []

    if argument.lower() in {"skip", "none", "never", "nahi", "no"}:
        row.status = DealerPortalVendorMapStatus.SKIPPED
        row.confirmed_at = now_ist_naive()
        row.confirmed_by = "admin"
        session.flush()
        return True, (
            f"✅ {row.vendor_name} ka stock ab kabhi Dealer Portal par push nahi hoga.\n"
            "Badalne ke liye batayiye."
        )

    if not argument.isdigit():
        return True, (
            'Samajh nahi aaya. "dp 1" (list ka number), "dp 1367" (dealer id), '
            'ya "dp skip" bhejiye.'
        )

    number = int(argument)
    chosen: dict | None = None
    # A small number is a pick from the list; anything larger is a dealer id.
    if 1 <= number <= len(candidates):
        chosen = candidates[number - 1]
    else:
        chosen = next(
            (c for c in candidates if int(c.get("dealer_id") or 0) == number), None
        )
        if chosen is None:
            # An id that was not on the list is still allowed -- the admin may
            # know better than the name search.
            chosen = {"dealer_id": number, "name": None, "type": None,
                      "own_warehouse": False}

    if chosen.get("own_warehouse"):
        return True, (
            f"⛔ [{chosen['dealer_id']}] {chosen.get('name')} ek CarTrends warehouse "
            "hai — uska stock DP se hi aata hai, wapas push karne se double ho "
            'jayega.\nDoosra number chuniye, ya "dp skip".'
        )

    row.dp_dealer_id = int(chosen["dealer_id"])
    row.dp_dealer_name = chosen.get("name")
    row.dp_dealer_type = chosen.get("type")
    row.status = DealerPortalVendorMapStatus.CONFIRMED
    row.confirmed_at = now_ist_naive()
    row.confirmed_by = "admin"
    session.flush()

    shown = row.dp_dealer_name or f"dealer {row.dp_dealer_id}"
    logger.info(
        "Dealer Portal mapping confirmed: vendor %s (%s) -> dealer %s (%s).",
        row.vendor_id,
        row.vendor_name,
        row.dp_dealer_id,
        shown,
    )
    return True, (
        f"✅ {row.vendor_name} → [{row.dp_dealer_id}] {shown}\n"
        "Yaad rakh liya. Ab dobara nahi poochunga."
    )


def pending_count(session) -> int:
    return len(
        session.execute(
            select(DealerPortalVendorMap).where(
                DealerPortalVendorMap.status == DealerPortalVendorMapStatus.PENDING
            )
        ).scalars().all()
    )
