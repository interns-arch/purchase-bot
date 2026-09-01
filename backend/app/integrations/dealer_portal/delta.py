"""Previous snapshot vs new snapshot -> the rows to push to Dealer Portal.

WHY THIS MODULE EXISTS
----------------------
Dealer Portal's upload is a `partial_absolute_upsert`. A part PRESENT in the
CSV has its quantity replaced; a part ABSENT from the CSV is left unchanged
FOREVER -- not zeroed, not deleted.

Vendor files are daily snapshots. Northend sends 500 parts today and 480
tomorrow because 20 sold out. Those 20 are simply absent from tomorrow's
file, so DP keeps telling the sales bot they are in stock. The bot promises
the customer, the PO fails, the customer is angry.

The fix is to send the sold-out parts EXPLICITLY, with quantity 0. To know
which parts those are you need yesterday's list, and DP cannot give it to
you (`GET /stock/` is HTTP 500 on the live environment, and there is no
"list everything this dealer has" call). ProcureHub already has it.

WHERE THE SNAPSHOT PAIR COMES FROM
----------------------------------
`InventoryImport` carries a unique partial index --
`ux_inventory_imports_one_active_per_vendor` (`core/models.py:227`) -- so
exactly one import is ACTIVE per vendor. When a new file arrives,
`_activate()` (`core/services/inventory_import_service.py:335`) marks the
previous import SUPERSEDED and the new one active. The superseded import's
`VendorInventory` rows are left in the database untouched.

That is the snapshot pair, already built. Nothing new has to be recorded.

THE UNION, AND WHY IT MATTERS
-----------------------------
The delta runs over the UNION of every ProcureHub vendor row in the account
group, not one vendor at a time -- see `credentials.py` for the full
reasoning. `Bijvasan` and `Bijwasan` are two vendor rows for one real
warehouse with one DP account; computing the delta per vendor row would zero
parts the other row still stocks.

    new_rows  = VendorInventory of the ACTIVE import of EVERY vendor in group
    prev_rows = VendorInventory of the PREVIOUS import of EVERY vendor in group

    push  = [(part, qty, price) for part in new_rows]
    push += [(part, 0,  price)  for part in prev_rows but NOT in new_rows]

That second line is the entire reason this project exists.

PRICE IS CARRIED FORWARD, NEVER INVENTED
----------------------------------------
DP replaces price with whatever we send, so a row with no price in
ProcureHub must not be sent as price 0 -- that would set the dealer price to
zero in the system the sales bot quotes from. Instead the last known price
for that part (from the previous snapshot) is carried forward, and only a
part that has never had a price falls back to 0. Those are counted in
`priceless_count` so shadow mode surfaces them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from backend.app.integrations.dealer_portal.credentials import DealerPortalAccount
from core.logging_setup import get_logger
from core.models import ImportStatus, InventoryImport, VendorInventory

logger = get_logger(__name__)

# Statuses an import may hold once it has finished and could be the previous
# snapshot. A FAILED or CANCELLED import never held stock.
_SETTLED_STATUSES = (
    ImportStatus.COMPLETED,
    ImportStatus.COMPLETED_WITH_ERRORS,
    ImportStatus.SUPERSEDED,
)


@dataclass
class DeltaRow:
    part_no: str
    quantity: int
    price: Decimal
    is_zeroed: bool = False


@dataclass
class DeltaResult:
    account_key: str
    rows: list[DeltaRow] = field(default_factory=list)
    vendor_ids: list[int] = field(default_factory=list)
    import_ids: list[int] = field(default_factory=list)
    zeroed_count: int = 0
    # Parts that sold out but were NOT zeroed because the account is not
    # marked `full_snapshot`. Logged so the Founder can see what turning the
    # flag on would do.
    withheld_zero_count: int = 0
    duplicate_count: int = 0
    priceless_count: int = 0

    @property
    def rows_sent(self) -> int:
        return len(self.rows)


def _active_import(vendor_id: int, session) -> InventoryImport | None:
    return (
        session.query(InventoryImport)
        .filter(InventoryImport.vendor_id == vendor_id)
        .filter(InventoryImport.is_active.is_(True))
        .one_or_none()
    )


def _previous_import(
    vendor_id: int, active: InventoryImport | None, session
) -> InventoryImport | None:
    """The import this vendor's active import superseded -- the most recent
    settled import that is not the active one."""
    query = (
        session.query(InventoryImport)
        .filter(InventoryImport.vendor_id == vendor_id)
        .filter(InventoryImport.status.in_(_SETTLED_STATUSES))
    )
    if active is not None:
        query = query.filter(InventoryImport.id != active.id)
    return query.order_by(
        InventoryImport.created_at.desc(), InventoryImport.id.desc()
    ).first()


def _collapse(
    rows: list[VendorInventory], strategy: str
) -> tuple[dict[str, Decimal], dict[str, Decimal], int]:
    """Collapse raw inventory rows to one entry per part number.

    DP itself SUMS duplicate rows for the same part, which silently inflates
    stock if our parser emits a part twice. We never inherit that -- the
    strategy is explicit and configurable, defaulting to `max` (the largest
    single stated availability wins, so a duplicate can never inflate).

    Returns (quantity_by_part, price_by_part, duplicates_collapsed)."""
    quantities: dict[str, Decimal] = {}
    prices: dict[str, Decimal] = {}
    duplicates = 0

    for row in rows:
        part = (row.normalized_part_number or "").strip()
        if not part:
            continue
        quantity = row.quantity_available or Decimal(0)

        if part in quantities:
            duplicates += 1
            if strategy == "sum":
                quantities[part] = quantities[part] + quantity
            elif strategy == "first":
                pass
            else:  # "max" -- the safe default
                quantities[part] = max(quantities[part], quantity)
        else:
            quantities[part] = quantity

        # Price: the highest non-null price seen for the part, matching DP's
        # own "max price wins" tie-break for duplicates.
        if row.price is not None:
            prices[part] = max(prices.get(part, row.price), row.price)

    return quantities, prices, duplicates


def build_delta(account: DealerPortalAccount, session) -> DeltaResult:
    """The rows to push for one Dealer Portal account, over the UNION of
    every ProcureHub vendor row in its group.

    Pure computation -- reads the database, writes nothing, makes no network
    call."""
    result = DeltaResult(account_key=account.key, vendor_ids=list(account.vendor_ids))

    new_rows: list[VendorInventory] = []
    prev_rows: list[VendorInventory] = []

    for vendor_id in account.vendor_ids:
        active = _active_import(vendor_id, session)
        previous = _previous_import(vendor_id, active, session)

        if active is not None:
            result.import_ids.append(active.id)
            new_rows.extend(
                session.query(VendorInventory)
                .filter(VendorInventory.import_id == active.id)
                .all()
            )
        if previous is not None:
            prev_rows.extend(
                session.query(VendorInventory)
                .filter(VendorInventory.import_id == previous.id)
                .all()
            )
        if active is None:
            logger.info(
                "Dealer Portal %s: vendor %s has no active import -- contributing "
                "nothing to this push.",
                account.key,
                vendor_id,
            )

    strategy = dealer_portal_settings.duplicate_strategy
    new_qty, new_price, new_dupes = _collapse(new_rows, strategy)
    prev_qty, prev_price, _ = _collapse(prev_rows, strategy)
    result.duplicate_count = new_dupes

    # 1. Everything in the new snapshot, at its stated quantity.
    for part, quantity in new_qty.items():
        price = new_price.get(part)
        if price is None:
            price = prev_price.get(part)  # carry forward, never invent a 0
            if price is None:
                price = Decimal(0)
                result.priceless_count += 1
        result.rows.append(
            DeltaRow(part_no=part, quantity=int(quantity), price=price)
        )

    # 2. THE WHOLE POINT: parts we sold yesterday and no longer stock.
    sold_out = [part for part in prev_qty if part not in new_qty]
    if account.full_snapshot:
        for part in sold_out:
            result.rows.append(
                DeltaRow(
                    part_no=part,
                    quantity=0,
                    price=prev_price.get(part, Decimal(0)),
                    is_zeroed=True,
                )
            )
        result.zeroed_count = len(sold_out)
    else:
        # The vendor is not confirmed to send a COMPLETE daily list, so a
        # missing part may just mean they sent one brand today. Zeroing then
        # would wipe their real stock in DP. Report, do not act.
        result.withheld_zero_count = len(sold_out)
        if sold_out:
            logger.info(
                "Dealer Portal %s: %d part(s) disappeared since the previous "
                "snapshot but were NOT zeroed -- DEALER_PORTAL_%s_FULL_SNAPSHOT "
                "is false. Set it to true once this vendor is confirmed to send a "
                "complete daily list.",
                account.key,
                len(sold_out),
                account.key,
            )

    if not prev_rows and new_rows:
        logger.warning(
            "Dealer Portal %s: no PREVIOUS snapshot found, so nothing can be "
            "zeroed on this push. If the vendor data was purged, run "
            "backend/scripts/dealer_portal_resync.py once the next full file "
            "has been imported.",
            account.key,
        )

    return result
