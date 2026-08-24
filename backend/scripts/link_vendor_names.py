"""Teach the system that a SPELLING belongs to an existing vendor.

    python -m backend.scripts.link_vendor_names 74 "BIJVASAN" "Bijwasan"
    python -m backend.scripts.link_vendor_names --list

Case and filler words already match on their own: 'JAIPUR STOCK', 'jaipur
stock' and 'jaipur' are one vendor without anyone declaring anything. This
script is for spellings no rule can safely guess -- 'BIJVASAN' vs
'BIJWASHAN' (V/W and a missing H), 'northern distributer' vs 'Northend
Distributors'. Automatic fuzzy matching is deliberately NOT used: at that
looseness it would also merge 'aman' with 'amit'.

After declaring, any future file captioned with that spelling imports under
the vendor you kept, instead of creating another duplicate.

IMPORTANT -- this does not move existing stock. If the other spelling is
already a real vendor with its own imports, the script says so and you want
`merge_vendors.py` instead, which moves everything and then records the
alias automatically.

Run with the production DATABASE_URL to apply it to production."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Same DATABASE_URL resolution as the app: backend/.env (real env wins).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from core.db import get_session  # noqa: E402
from core.services import vendor_service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vendor_id", nargs="?", type=int, help="Vendor id to KEEP")
    parser.add_argument("aliases", nargs="*", help="Spelling(s) that mean that vendor")
    parser.add_argument("--list", action="store_true", help="Show declared spellings")
    parser.add_argument("--declared-by", default="cli")
    args = parser.parse_args()

    with get_session() as session:
        if args.list:
            rows = vendor_service.list_vendor_name_aliases(session)
            if not rows:
                print("No vendor spellings declared yet.")
                return 0
            print(f"{len(rows)} declared spelling(s):")
            for alias, vendor_name in rows:
                print(f"  {alias!r} -> {vendor_name}")
            return 0

        if args.vendor_id is None or not args.aliases:
            parser.error("Give a vendor id and at least one spelling, or --list.")

        failed = False
        for alias in args.aliases:
            try:
                result = vendor_service.remember_vendor_name(
                    alias, args.vendor_id, session, declared_by=args.declared_by
                )
            except vendor_service.VendorNameInUseError as exc:
                failed = True
                print(f"  SKIPPED {alias!r}: {exc}")
                print(
                    f"          Both hold their own stock, so merge them instead:\n"
                    f"          python -m backend.scripts.merge_vendors "
                    f"{args.vendor_id} {exc.existing.id}"
                )
                continue
            except ValueError as exc:
                failed = True
                print(f"  SKIPPED {alias!r}: {exc}")
                continue
            print(f"  {result['action']}: {result['alias']!r} -> {result['vendor']}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
