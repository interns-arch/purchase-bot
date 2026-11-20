"""Link vendors the bot created on the Dealer Portal to their Portal dealer
and save their login (20 Nov 2026). The logic is
`vendor_onboarding.portal_link.backfill`, which also runs every evening by
itself (scheduler job `vendor_portal_link`).

    python backend/scripts/link_created_vendors.py            # dry run: shows what it would do
    python backend/scripts/link_created_vendors.py --apply    # writes the links

Only a login the BOT made is used, and only after ONE test login confirms the
dealer id. Prints no password. Already-linked vendors are skipped."""

from __future__ import annotations

import sys


def main(apply: bool) -> int:
    from backend.app.vendor_onboarding import portal_link

    print("APPLY -- links will be saved" if apply else "DRY RUN -- nothing is saved (add --apply)")
    lines = portal_link.backfill(apply=apply)
    for line in lines:
        print(line)
    problems = sum(1 for line in lines if "NOT linked" in line)
    print(f"\n{'done' if apply else 'dry run done'}; {problems} need a person")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv[1:]))
