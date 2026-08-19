"""Declare that two part numbers are the SAME physical part.

    python -m backend.scripts.link_part_numbers MF390300ML32 MF390300ML
    python -m backend.scripts.link_part_numbers --list

The Founder's rule: some vendors write a part number with a suffix and
others without it, and only a human knows the two mean one item. Nothing is
ever guessed -- 'ML32' and 'ML33' stay different parts unless someone says
otherwise here.

Once linked, both numbers match the same inventory and share ONE reservation
ledger everywhere: vendor comparison, automatic allocation, top-up, stock
gaps and part intelligence. Linking is idempotent, order-independent, and
transitive (A=B then B=C puts all three together).

Run with the production DATABASE_URL to apply it to production."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Same DATABASE_URL resolution as the app: backend/.env (real env wins).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from core.db import get_session  # noqa: E402
from core.services import part_link_service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", nargs="?", help="One part number")
    parser.add_argument("second", nargs="?", help="The number that means the same part")
    parser.add_argument("--list", action="store_true", help="Show every declared equivalence")
    parser.add_argument("--declared-by", default="cli", help="Who declared it (for the record)")
    args = parser.parse_args()

    with get_session() as session:
        if args.list:
            groups = part_link_service.list_links(session)
            if not groups:
                print("No part-number equivalences declared yet.")
                return 0
            print(f"{len(groups)} declared equivalence(s):")
            for _group_key, numbers in groups:
                print("  " + " = ".join(numbers))
            return 0

        if not args.first or not args.second:
            parser.error("Give two part numbers, or --list.")

        result = part_link_service.link_part_numbers(
            args.first, args.second, session, declared_by=args.declared_by
        )

    print(f"{result['action']}: " + " = ".join(result["numbers"]))
    if result["action"] == "already_linked":
        print("(nothing to do -- these numbers were already the same part)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
