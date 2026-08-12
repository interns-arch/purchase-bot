"""Bulk-register vendor WhatsApp numbers from an Excel contact list.

    python -m backend.scripts.register_vendor_numbers "path\\to\\contacts.xlsx" [--dry-run]

Thin CLI wrapper around the SAME parser + apply logic the Founder's
WhatsApp "register" flow uses (`integrations.whatsapp.contact_import`), so
the two can never disagree:

- vendor name in the first column; numbers from an "updated ..." column when
  one exists (a sheet carrying both old and corrected numbers uses the
  corrected ones), else phone/contact-titled columns, else every cell after
  the name;
- each listed vendor's numbers REPLACE its previous registrations;
- a number already seen on an earlier row means the SAME party -- the later
  row is skipped, no second vendor is created (e.g. Brite Autocars /
  Brite Autowheels sharing one number);
- unknown vendor names are onboarded with a permanent Vendor Code.

Uses DATABASE_URL exactly like the app (backend/.env is honoured), so run it
locally against the local DB, or with the production DATABASE_URL to seed
production -- deliberately the admin's explicit choice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.app.integrations.whatsapp import contact_import
from core.db import get_session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", help="Excel file: vendor name + phone number columns")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing anything.",
    )
    args = parser.parse_args()

    path = Path(args.workbook)
    if not path.exists():
        print(f"File not found: {path}")
        return 1

    rows = contact_import.parse_contact_rows(path)
    print(f"{len(rows)} vendor row(s) found in {path.name}.\n")
    if not rows:
        return 1

    with get_session() as session:
        report, stats = contact_import.apply_contact_update(rows, session)
        if args.dry_run:
            session.rollback()

    print(report)
    if args.dry_run:
        print("\nDRY RUN -- nothing was saved.")
    print(f"\nStats: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
