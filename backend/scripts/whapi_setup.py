"""Set up the bot's WhatsApp groups on Whapi.Cloud (27 Oct 2026).

Run inside the backend container once WHAPI_TOKEN is in backend/.env:

    # is the linked phone connected? which groups is it in (name + id)?
    python -m backend.scripts.whapi_setup

    # point Whapi's webhook at the bot (uses WHAPI_WEBHOOK_SECRET)
    python -m backend.scripts.whapi_setup --set-webhook https://partsexpressonline.com/api/whapi/webhook

Then put the group ids in WHAPI_TEAM_GROUP / WHAPI_VENDOR_GROUPS and restart.
Never prints the token.
"""

from __future__ import annotations

import argparse
import sys

from backend.app.integrations.whatsapp import whapi


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-webhook", metavar="URL")
    args = parser.parse_args()

    if not whapi.enabled():
        print("WHAPI_ENABLED is not true or WHAPI_TOKEN is missing in backend/.env.")
        return 1
    health = whapi.health()
    status = (health.get("status") or {}).get("text") or health.get("status")
    user = health.get("user") or {}
    print(f"Channel status: {status}   linked number: {user.get('id', '?')} ({user.get('name', '')})")

    if args.set_webhook:
        if not whapi.webhook_secret():
            print("Set WHAPI_WEBHOOK_SECRET in backend/.env first.")
            return 1
        whapi.set_webhook(args.set_webhook)
        print(f"Webhook set to {args.set_webhook} (messages only, with the secret header).")

    configured = whapi.allowed_groups()
    print("\nGroups this number is in:")
    for group in whapi.list_groups():
        mark = "  <- used by the bot" if group.get("id") in configured else ""
        print(f"  {group.get('id')}   {group.get('name')}   ({len(group.get('participants') or [])} people){mark}")
    print(f"\nTeam group:    {whapi.team_group() or '-'}")
    for code, group in whapi.vendor_groups().items():
        print(f"Vendor group:  {code} -> {group}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
