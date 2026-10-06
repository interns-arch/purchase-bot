"""One-off (5 Oct 2026): rename every vendor code to the Boodmo-style CT_
prefix (ND_CT -> CT_ND, MA_CT_2 -> CT_MA_2) and remove the old TEST vendors.

    python -m backend.scripts.migrate_vendor_codes_ct_prefix            # dry run
    python -m backend.scripts.migrate_vendor_codes_ct_prefix --apply

Old-style codes in filenames keep working (`vendor_code_service.canonical_code`).
A test vendor is deleted only when nothing but its own stock imports points
at it -- one with orders, allocations, POs or invoices is skipped and reported.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from core.db import get_session
from core.services.vendor_code_service import canonical_code

TEST_VENDOR_NAMES = [
    "V01_Apex_clear", "V02_Bharat_aliases", "V03_Central_column_order", "V04_Delta_metadata",
    "V05_Everest_sku_price_fields", "V06_Falcon_ambiguous_headers", "V07_Gamma_missing_qty",
    "V08_Horizon_duplicate_upload", "V08_Horizon_duplicate_upload_CHANGED",
    "V09_Indus_metadata_and_column_mix", "V10_Jupiter_price_mistake_trap", "VendorD_Spares",
    "aman", "aman traders", "amit", "demo motor", "demo vendor", "stock10", "test2_stock_7",
    "test_1_MAHINDRA_STOCK_08-08-2026", "test_3_STOCK_8-AUGUST", "Anuj",
]
# Rows of these tables belong to the vendor's own stock imports and go with it.
OWN_DATA_TABLES = {"inventory_imports", "vendor_inventory", "import_errors"}


def _vendor_fk_tables(session) -> list[tuple[str, str]]:
    return session.execute(
        text(
            """
            select tc.table_name, kcu.column_name
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu
              on tc.constraint_name = kcu.constraint_name and tc.table_schema = kcu.table_schema
            join information_schema.constraint_column_usage ccu
              on ccu.constraint_name = tc.constraint_name and ccu.table_schema = tc.table_schema
            where tc.constraint_type = 'FOREIGN KEY' and ccu.table_name = 'vendors'
            """
        )
    ).all()


def main(apply: bool) -> None:
    with get_session() as session:
        # ---- 1. codes
        vendors = session.execute(text("select id, name, vendor_code from vendors order by id")).all()
        renames = []
        for vendor_id, name, code in vendors:
            if code and canonical_code(code) != code:
                renames.append((vendor_id, name, code, canonical_code(code)))
        new_codes = [r[3] for r in renames] + [c for _, _, c in vendors if c and canonical_code(c) == c]
        assert len(new_codes) == len(set(new_codes)), "code collision -- aborting"
        print(f"Code renames: {len(renames)}")
        for vendor_id, name, old, new in renames:
            print(f"  #{vendor_id:<3} {old:<10} -> {new:<10} {name}")

        # ---- 2. test vendors
        fks = _vendor_fk_tables(session)
        print(f"\nTest vendors to delete ({len(TEST_VENDOR_NAMES)} names):")
        deletable = []
        for name in TEST_VENDOR_NAMES:
            row = session.execute(text("select id from vendors where name = :n"), {"n": name}).first()
            if row is None:
                print(f"  - {name}: not found")
                continue
            vendor_id = row[0]
            blockers = []
            for table, column in fks:
                if table in OWN_DATA_TABLES:
                    continue
                count = session.execute(
                    text(f'select count(*) from "{table}" where "{column}" = :v'), {"v": vendor_id}
                ).scalar()
                if count:
                    blockers.append(f"{table}={count}")
            if blockers:
                print(f"  SKIP #{vendor_id} {name}: still referenced ({', '.join(blockers)})")
            else:
                deletable.append((vendor_id, name))
                print(f"  delete #{vendor_id} {name}")

        if not apply:
            print("\nDRY RUN -- nothing changed. Re-run with --apply.")
            session.rollback()
            return

        for vendor_id, _name, _old, new in renames:
            session.execute(text("update vendors set vendor_code = :c where id = :v"), {"c": new, "v": vendor_id})
        for vendor_id, _name in deletable:
            import_ids = [r[0] for r in session.execute(
                text("select id from inventory_imports where vendor_id = :v"), {"v": vendor_id})]
            for import_id in import_ids:
                session.execute(text("delete from vendor_inventory where import_id = :i"), {"i": import_id})
                session.execute(text("delete from import_errors where import_id = :i"), {"i": import_id})
            session.execute(text("delete from vendor_inventory where vendor_id = :v"), {"v": vendor_id})
            session.execute(text("delete from inventory_imports where vendor_id = :v"), {"v": vendor_id})
            session.execute(text("delete from vendors where id = :v"), {"v": vendor_id})
        print(f"\nAPPLIED: {len(renames)} code(s) renamed, {len(deletable)} test vendor(s) deleted.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
