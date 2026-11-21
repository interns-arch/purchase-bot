"""Map the brands of vendors the bot created onto the Dealer Portal (21 Nov 2026).
The logic is vendor_onboarding.portal_brands.backfill: form brands -> the Portal's
exact brand names -> mapping (vendor-mappings) + discount (vendor-brand-discounts),
each read back, and the bot's own brand list.

    python backend/scripts/map_vendor_brands.py            # dry run
    python backend/scripts/map_vendor_brands.py --apply    # writes

Brands already mapped / discounts already equal are left alone."""

from __future__ import annotations

import sys


def main(apply: bool) -> int:
    from backend.app.vendor_onboarding import portal_brands

    print("APPLY -- Portal + bot are written" if apply else "DRY RUN -- nothing is written (add --apply)")
    for line in portal_brands.backfill(apply=apply):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv[1:]))
