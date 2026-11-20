"""Link vendors the bot already created on the Dealer Portal to their Portal
dealer (one-off backfill, 20 Nov 2026). See vendor_onboarding/portal_link.py.

    python backend/scripts/link_created_vendors.py            # dry run: shows what it would do
    python backend/scripts/link_created_vendors.py --apply    # writes the links

For every CREATED registration with a "DP-..." Portal reference:
  1. the Portal's own user list (admin login) gives the vendor's login and its
     DEALER id -- found by the vendor's mobile number;
  2. only a login the BOT made is used (its username is one the bot would have
     picked, its password the bot's rule) -- an older account someone else
     made (e.g. VR-24, "DP-EXISTS-...") is reported and left alone;
  3. ONE test login (no retry) must say the same dealer id before anything is
     saved. A wrong password is reported, never guessed again;
  4. then the stock-upload map + the accounts-file login are saved.
Prints no password. Already-linked vendors are skipped."""

from __future__ import annotations

import base64
import json
import sys

from sqlalchemy import select


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


def _digits10(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]


def main(apply: bool) -> int:
    from backend.app.integrations.dealer_portal import client as dp_client, mapping
    from backend.app.integrations.dealer_portal.config import dealer_portal_settings
    from backend.app.integrations.dealer_portal.credentials import DealerPortalAccount
    from backend.app.vendor_onboarding import models as m, portal_link
    from backend.app.vendor_onboarding import portal_vendor_api as api
    from core.db import get_session
    from core.models import Vendor

    print("APPLY -- links will be saved" if apply else "DRY RUN -- nothing is saved (add --apply)")
    token = api.login()
    try:
        users = api._get_rows(token, "/users/?limit=10000")  # noqa: SLF001
    finally:
        api.logout(token)

    problems = 0
    with get_session() as session:
        requests = session.execute(
            select(m.VendorRegistrationRequest)
            .where(m.VendorRegistrationRequest.status == m.REG_CREATED,
                   m.VendorRegistrationRequest.dealer_portal_ref.like("DP-%"))
            .order_by(m.VendorRegistrationRequest.id)
        ).scalars().all()
        for request in requests:
            code = f"VR-{request.id}"
            vendor = session.get(Vendor, request.vendor_id) if request.vendor_id else None
            if vendor is None:
                print(f"{code}: no ProcureHub vendor row -- skipped")
                problems += 1
                continue
            label = f"{code} {vendor.name} ({vendor.vendor_code})"
            if mapping.is_resolved(mapping.get_map(vendor.id, session)):
                print(f"{label}: already linked -- skipped")
                continue
            answers = request.answers or {}
            mobile = _digits10(answers.get("mobile"))
            hits = [u for u in users if mobile and _digits10(u.get("phone")) == mobile]
            mine = set(api.username_candidates(answers, request.gst_data))
            ours = [u for u in hits if str(u.get("username") or "").strip().lower() in mine]
            if not ours:
                found = ", ".join(f"{u.get('username')} (dealer {u.get('dealer_id')})" for u in hits) or "nothing"
                print(f"{label}: no login made by the bot for mobile {mobile} (Portal has: {found}) -- NOT linked, check by hand")
                problems += 1
                continue
            user = ours[0]
            dealer_id = int(user["dealer_id"])
            username = str(user["username"]).strip()
            password = api.password_for(answers)
            account = DealerPortalAccount(
                key=portal_link.account_key(vendor), username=username, password=password, token=None,
                device_id=f"procurehub-{portal_link.account_key(vendor).lower()}", vendor_patterns=[vendor.vendor_code],
                full_snapshot=True, tat_days=dealer_portal_settings.tat_days, enabled=True,
            )
            try:
                jwt = dp_client._login(account)  # noqa: SLF001 -- one attempt, no retry
            except dp_client.DealerPortalError as exc:
                print(f"{label}: test login as {username} FAILED ({str(exc)[:80]}) -- NOT linked, check the password by hand")
                problems += 1
                continue
            claims = _claims(jwt)
            try:
                dp_client._requests().post(f"{dealer_portal_settings.base_url}/auth/logout",  # noqa: SLF001
                                           headers={"Authorization": f"Bearer {jwt}"},
                                           timeout=dealer_portal_settings.timeout_seconds)
            except Exception:  # noqa: BLE001
                pass
            if str(claims.get("dealer_id")) != str(dealer_id):
                print(f"{label}: login says dealer {claims.get('dealer_id')}, user list says {dealer_id} -- NOT linked")
                problems += 1
                continue
            if "stock.upload" not in (claims.get("permissions") or []):
                print(f"{label}: dealer {dealer_id} has no stock-upload permission -- linked for PO-MST only")
                username = password = None
            if apply:
                done = portal_link.link_vendor(vendor, dealer_id, session, username=username, password=password,
                                               dealer_name=claims.get("display_name") or vendor.name)
                print(f"{label}: dealer {dealer_id} ({claims.get('display_name')}) -- {'; '.join(done)}")
            else:
                print(f"{label}: would link to dealer {dealer_id} ({claims.get('display_name')}), login {username} checked OK")
        if apply:
            session.commit()
    print(f"\n{'done' if apply else 'dry run done'}; {problems} need a person")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv[1:]))
