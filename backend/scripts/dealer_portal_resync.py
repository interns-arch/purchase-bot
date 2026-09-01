"""Force a full Dealer Portal push for one account (or every account).

WHEN YOU NEED THIS
------------------
1. AFTER A PURGE. `data_purge_service._purge_vendor()` deletes every
   `InventoryImport` and `VendorInventory` row -- for ALL vendors, not one.
   With no previous snapshot the next push zeroes nothing, so DP silently
   keeps stale stock for the whole vendor base. Re-import the vendors' current
   files, then run this with `--full` so the sold-out parts are zeroed.

2. AFTER FIXING A MAPPING. A vendor added to a
   `DEALER_PORTAL_<KEY>_VENDORS` group after the fact has never been pushed;
   this sends its stock without waiting for tomorrow's file.

3. TO CHECK A CONFIGURATION. With `--dry-run` (or DEALER_PORTAL_SHADOW=true)
   nothing is sent -- it prints exactly what would be, including which
   ProcureHub vendor rows each account covers.

USAGE
-----
    # what would be sent for every configured account, no network call
    python -m backend.scripts.dealer_portal_resync --dry-run

    # one account, live
    python -m backend.scripts.dealer_portal_resync --account BIJWASAN

    # one account, zeroing sold-out parts even if FULL_SNAPSHOT is false
    python -m backend.scripts.dealer_portal_resync --account BIJWASAN --full

    # every configured account
    python -m backend.scripts.dealer_portal_resync --all

`--full` overrides `DEALER_PORTAL_<KEY>_FULL_SNAPSHOT` for this run only.
Only use it for a vendor whose latest import really is a COMPLETE list --
on a partial file it would wipe stock that vendor genuinely has.

Re-running is harmless: DP's upload is an absolute replace, so the same CSV
sent twice cannot double-count.
"""

from __future__ import annotations

import argparse
import sys

from backend.app.integrations.dealer_portal import credentials, csv_builder, delta
from backend.app.integrations.dealer_portal import push_service
from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)


def _describe(account, session) -> None:
    result = delta.build_delta(account, session)
    print(f"\n=== {account.key} ===")
    print(f"  ProcureHub vendor rows : {', '.join(account.vendor_names) or '(none matched)'}")
    print(f"  vendor ids             : {account.vendor_ids}")
    print(f"  active import ids      : {result.import_ids}")
    print(f"  full_snapshot          : {account.full_snapshot}")
    print(f"  rows to send           : {result.rows_sent}")
    print(f"  of which zeroed        : {result.zeroed_count}")
    print(f"  sold out, NOT zeroed   : {result.withheld_zero_count}")
    print(f"  duplicates collapsed   : {result.duplicate_count}")
    print(f"  no known price (sent 0): {result.priceless_count}")
    print("  CSV preview:")
    for line in csv_builder.preview(result.rows, tat_days=account.tat_days).splitlines():
        print(f"    {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--account", help="one DEALER_PORTAL_ACCOUNTS key, e.g. BIJWASAN")
    group.add_argument("--all", action="store_true", help="every configured account")
    parser.add_argument(
        "--full",
        action="store_true",
        help="zero sold-out parts even if FULL_SNAPSHOT is false (this run only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent and make no network call",
    )
    args = parser.parse_args(argv)

    if not dealer_portal_settings.enabled:
        print(
            "DEALER_PORTAL_ENABLED is not true -- nothing to do. Set it in "
            "backend/.env first.",
            file=sys.stderr,
        )
        return 1

    with get_session() as session:
        if args.all:
            accounts = [
                credentials.resolve_account_vendors(a, session)
                for a in credentials.load_accounts()
                if a.enabled
            ]
        else:
            account = credentials.account_by_key(args.account, session)
            if account is None:
                print(
                    f"No Dealer Portal account named {args.account!r}. Configured: "
                    f"{', '.join(dealer_portal_settings.account_keys) or '(none)'}",
                    file=sys.stderr,
                )
                return 1
            accounts = [account]

        if not accounts:
            print("No enabled Dealer Portal accounts are configured.", file=sys.stderr)
            return 1

        if args.dry_run:
            for account in accounts:
                if args.full:
                    account.full_snapshot = True
                _describe(account, session)
            print("\n(dry run -- nothing was sent)")
            return 0

    for account in accounts:
        status = push_service.push_account(account, force_full_snapshot=args.full)
        print(f"{account.key}: {status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
