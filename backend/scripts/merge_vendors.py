"""Merge a duplicate vendor into the real one.

    python -m backend.scripts.merge_vendors <keep_id> <duplicate_id> [--dry-run]

Fixes the Founder's "Bijwasan vs BIJWASHAN STOCK" problem for rows that
ALREADY exist: everything the duplicate owns is moved onto the vendor being
kept, then the duplicate row is deleted. Going forward the name matcher
ignores filler words like "Stock", so new duplicates stop appearing; this
script is only for cleaning up ones created before that fix.

Moved (with uniqueness conflicts resolved in the keeper's favour):
  inventory imports + inventory rows, part aliases, vendor selections,
  WhatsApp registered numbers, purchase orders, invoice imports,
  delivery items, WhatsApp vendor-memory rows.

If BOTH vendors currently have an ACTIVE inventory import, the duplicate's
active import is marked SUPERSEDED (the keeper's active batch stays the
truth). Audited. Run with the production DATABASE_URL to fix production.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Same DATABASE_URL resolution as the app: backend/.env (real env wins).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import select, update  # noqa: E402

from core.db import get_session
from core.models import (
    ImportStatus,
    InventoryImport,
    PartAlias,
    Vendor,
    VendorInventory,
    VendorInvoiceImport,
    VendorPurchaseOrder,
    VendorSelection,
)


def merge(keep_id: int, duplicate_id: int, session, *, verbose: bool = True) -> dict:
    keep = session.get(Vendor, keep_id)
    duplicate = session.get(Vendor, duplicate_id)
    if keep is None or duplicate is None:
        raise SystemExit("Both vendor ids must exist.")
    if keep_id == duplicate_id:
        raise SystemExit("keep_id and duplicate_id are the same vendor.")

    moved: dict[str, int] = {}

    # Active-import conflict: keep whichever batch is NEWER (a vendor's
    # latest upload is the truth, regardless of which duplicate row it
    # landed under) and supersede the older one.
    keep_active = session.execute(
        select(InventoryImport).where(
            InventoryImport.vendor_id == keep_id, InventoryImport.is_active.is_(True)
        )
    ).scalars().first()
    duplicate_active = session.execute(
        select(InventoryImport).where(
            InventoryImport.vendor_id == duplicate_id, InventoryImport.is_active.is_(True)
        )
    ).scalars().first()
    if keep_active is not None and duplicate_active is not None:
        older = (
            duplicate_active
            if (duplicate_active.created_at or 0) <= (keep_active.created_at or 0)
            else keep_active
        )
        older.is_active = False
        older.status = ImportStatus.SUPERSEDED
        moved["older_active_superseded"] = older.id

    moved["inventory_imports"] = session.execute(
        update(InventoryImport).where(InventoryImport.vendor_id == duplicate_id)
        .values(vendor_id=keep_id)
    ).rowcount
    moved["inventory_rows"] = session.execute(
        update(VendorInventory).where(VendorInventory.vendor_id == duplicate_id)
        .values(vendor_id=keep_id)
    ).rowcount

    # Part aliases: (vendor_id, normalized) is unique -- drop the duplicate's
    # alias where the keeper already has one for the same normalized number.
    keep_norms = set(session.execute(
        select(PartAlias.normalized_part_number).where(PartAlias.vendor_id == keep_id)
    ).scalars())
    dropped = alias_moved = 0
    for alias in session.execute(
        select(PartAlias).where(PartAlias.vendor_id == duplicate_id)
    ).scalars():
        if alias.normalized_part_number in keep_norms:
            session.delete(alias)
            dropped += 1
        else:
            alias.vendor_id = keep_id
            alias_moved += 1
    session.flush()
    moved["part_aliases_moved"] = alias_moved
    moved["part_aliases_dropped_dupes"] = dropped

    moved["vendor_selections"] = session.execute(
        update(VendorSelection).where(VendorSelection.vendor_id == duplicate_id)
        .values(vendor_id=keep_id)
    ).rowcount
    moved["purchase_orders"] = session.execute(
        update(VendorPurchaseOrder).where(VendorPurchaseOrder.vendor_id == duplicate_id)
        .values(vendor_id=keep_id)
    ).rowcount
    moved["invoice_imports"] = session.execute(
        update(VendorInvoiceImport).where(VendorInvoiceImport.vendor_id == duplicate_id)
        .values(vendor_id=keep_id)
    ).rowcount

    from backend.app.integrations.whatsapp.models import (
        WhatsAppRegisteredNumber,
        WhatsAppVendorMemory,
    )
    from core.models import VendorDeliveryItem

    moved["delivery_items"] = session.execute(
        update(VendorDeliveryItem).where(VendorDeliveryItem.vendor_id == duplicate_id)
        .values(vendor_id=keep_id)
    ).rowcount
    moved["registered_numbers"] = session.execute(
        update(WhatsAppRegisteredNumber)
        .where(WhatsAppRegisteredNumber.vendor_id == duplicate_id)
        .values(vendor_id=keep_id)
    ).rowcount
    session.execute(
        update(WhatsAppVendorMemory)
        .where(WhatsAppVendorMemory.vendor_name == duplicate.name)
        .values(vendor_name=keep.name)
    )

    # REMEMBER the duplicate's name forever (Founder's rule): any future
    # upload captioned with the old name resolves to the keeper instead of
    # onboarding a fresh duplicate -- even when spellings differ too much
    # for the filler-word matcher (Bijvasan vs BIJWASHAN).
    from core.models import VendorNameAlias
    from core.services.vendor_service import normalise_vendor_name

    duplicate_name = duplicate.name
    normalized = normalise_vendor_name(duplicate_name)
    if normalized and session.execute(
        select(VendorNameAlias).where(VendorNameAlias.normalized_name == normalized)
    ).scalar_one_or_none() is None:
        session.add(VendorNameAlias(normalized_name=normalized, vendor_id=keep_id))
        moved["name_alias_remembered"] = 1

    session.delete(duplicate)
    session.flush()

    from backend.app.services import audit_service

    audit_service.record(
        session,
        actor="merge-script",
        action="vendor_merge",
        entity_type="vendor",
        entity_id=keep_id,
        previous_value=f"duplicate vendor #{duplicate_id} '{duplicate_name}'",
        new_value=moved,
        reason=f"Merged into #{keep_id} '{keep.name}'",
    )
    if verbose:
        print(f"Merged '{duplicate_name}' (#{duplicate_id}) into '{keep.name}' (#{keep_id}):")
        for key, value in moved.items():
            print(f"  {key}: {value}")
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keep_id", type=int, help="Vendor id to KEEP")
    parser.add_argument("duplicate_id", type=int, help="Vendor id to merge and delete")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with get_session() as session:
        merge(args.keep_id, args.duplicate_id, session)
        if args.dry_run:
            session.rollback()
            print("\nDRY RUN -- nothing was saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
