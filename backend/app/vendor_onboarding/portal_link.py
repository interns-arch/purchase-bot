"""Link a vendor the bot created on the Dealer Portal to its Portal dealer
(20 Nov 2026).

Until now a vendor made through `POST /users/vendor/create` was only noted
on its registration ("DP-8790"), and that number was not even the dealer id.
Nothing used it: his stock sheets were never pushed (no login, no map) and a
Portal procurement win stopped at "vendor not linked to a Portal dealer yet".

`link_vendor` writes the two things the rest of the bot reads:

  * the stock-upload map (`dealer_portal_vendor_map`, CONFIRMED) -- also the
    first thing `portal_procurement.links.resolve` checks for a PO-MST;
  * the dealer login in the accounts file (DEALER_PORTAL_ACCOUNTS_FILE),
    exactly as the website's Dealer Portal page saves one. Credentials stay
    out of the database and out of the logs.

The login only pushes stock files received AFTER the link (`push_stock_after`),
so nothing old or sample-looking goes to the Portal before his first sheet.
Never raises; a vendor that cannot be linked is logged and left as before."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)


def account_key(vendor) -> str:
    """Same rule as the website's Dealer Portal page (`dealer_portal_accounts._key_for`)."""
    code = (vendor.vendor_code or "").upper().replace("CT_", "")
    base = code or vendor.name
    return re.sub(r"[^A-Z0-9]", "", base.upper())[:20] or f"V{vendor.id}"


def link_vendor(vendor, dealer_id: int, session: Session, *, username: str | None = None,
                password: str | None = None, dealer_name: str | None = None) -> list[str]:
    """Link `vendor` to Portal dealer `dealer_id`. Returns what was done, in
    words, for the log / the approver. Never raises."""
    from backend.app.integrations.dealer_portal import credentials, mapping
    from backend.app.integrations.dealer_portal.config import dealer_portal_settings

    done: list[str] = []
    try:
        mapping.confirm_from_login(vendor.id, vendor.name, int(dealer_id), dealer_name or vendor.name, session)
        session.flush()
        done.append(f"linked to dealer {dealer_id}")
    except Exception:  # noqa: BLE001
        logger.exception("Vendor %s: could not save the Portal dealer link %s.", vendor.id, dealer_id)
        return done
    if not (username and password):
        return done
    if not dealer_portal_settings.accounts_file:
        logger.warning("Vendor %s linked to dealer %s, but DEALER_PORTAL_ACCOUNTS_FILE is not set -- "
                       "his login was not saved, so his stock is not pushed.", vendor.id, dealer_id)
        return done
    try:
        for account in credentials.load_accounts():
            credentials.resolve_account_vendors(account, session)
            if vendor.id in account.vendor_ids or (account.dealer_id and int(account.dealer_id) == int(dealer_id)):
                done.append(f"stock login already set ({account.key})")
                return done
        key = account_key(vendor)
        taken = {a.key for a in credentials.load_accounts()}
        if key in taken:
            key = f"{key[:16]}{vendor.id}"
        credentials.save_file_account({
            "key": key,
            "username": username,
            "password": password,
            # The exact vendor code: codes are matched exactly, never by name,
            # so this login can never sweep in another vendor.
            "vendors": [vendor.vendor_code or vendor.name],
            "dealer_id": int(dealer_id),
            "dealer_name": dealer_name or vendor.name,
            "full_snapshot": True,
            "reset_before_upload": True,
            "enabled": True,
            "push_stock_after": now_ist_naive().replace(microsecond=0).isoformat(),
        })
        done.append(f"stock login saved ({key})")
    except Exception:  # noqa: BLE001
        logger.exception("Vendor %s: could not save the Portal stock login.", vendor.id)
    logger.info("Vendor %s (%s): %s.", vendor.id, vendor.name, "; ".join(done))
    return done


# ====================================================================== backfill / daily safety net
def _claims(token: str) -> dict:
    import base64
    import json

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


def _digits10(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]


def backfill(*, apply: bool) -> list[str]:
    """Every CREATED registration with a "DP-..." Portal reference whose vendor
    is not linked yet: find the login the BOT made (by his mobile, a username
    the bot would pick), check it with ONE test login (no retry), then link.
    An account someone else made, or a login that fails, is reported and left
    alone. Returns one line per vendor; prints / logs no password."""
    from sqlalchemy import select

    from backend.app.integrations.dealer_portal import client as dp_client, mapping
    from backend.app.integrations.dealer_portal.config import dealer_portal_settings
    from backend.app.integrations.dealer_portal.credentials import DealerPortalAccount
    from backend.app.vendor_onboarding import models as m
    from backend.app.vendor_onboarding import portal_vendor_api as api
    from core.db import get_session
    from core.models import Vendor

    out: list[str] = []
    with get_session() as session:
        requests = session.execute(
            select(m.VendorRegistrationRequest)
            .where(m.VendorRegistrationRequest.status == m.REG_CREATED,
                   m.VendorRegistrationRequest.dealer_portal_ref.like("DP-%"))
            .order_by(m.VendorRegistrationRequest.id)
        ).scalars().all()
        todo = []
        for request in requests:
            vendor = session.get(Vendor, request.vendor_id) if request.vendor_id else None
            if vendor is not None and not mapping.is_resolved(mapping.get_map(vendor.id, session)):
                todo.append((request, vendor))
            elif vendor is None:
                out.append(f"VR-{request.id}: no ProcureHub vendor row -- skipped")
        if not todo:
            return out + ["every vendor the bot created is linked"]
        token = api.login()
        try:
            users = api._get_rows(token, "/users/?limit=10000")  # noqa: SLF001
        finally:
            api.logout(token)
        for request, vendor in todo:
            label = f"VR-{request.id} {vendor.name} ({vendor.vendor_code})"
            answers = request.answers or {}
            mobile = _digits10(answers.get("mobile"))
            hits = [u for u in users if mobile and _digits10(u.get("phone")) == mobile]
            mine = set(api.username_candidates(answers, request.gst_data))
            ours = [u for u in hits if str(u.get("username") or "").strip().lower() in mine]
            if not ours:
                found = ", ".join(f"{u.get('username')} (dealer {u.get('dealer_id')})" for u in hits) or "nothing"
                out.append(f"{label}: no login made by the bot for mobile {mobile} (Portal has: {found}) -- NOT linked, check by hand")
                continue
            user = ours[0]
            if not str(user.get("dealer_id") or "").isdigit():
                out.append(f"{label}: login {user.get('username')} has no dealer id on the Portal -- NOT linked")
                continue
            dealer_id, username, password = int(user["dealer_id"]), str(user["username"]).strip(), api.password_for(answers)
            key = account_key(vendor)
            account = DealerPortalAccount(
                key=key, username=username, password=password, token=None, device_id=f"procurehub-{key.lower()}",
                vendor_patterns=[vendor.vendor_code], full_snapshot=True,
                tat_days=dealer_portal_settings.tat_days, enabled=True,
            )
            try:
                jwt = dp_client._login(account)  # noqa: SLF001 -- one attempt, no retry
            except dp_client.DealerPortalError as exc:
                out.append(f"{label}: test login as {username} FAILED ({str(exc)[:80]}) -- NOT linked, check the password by hand")
                continue
            claims = _claims(jwt)
            try:
                dp_client._requests().post(f"{dealer_portal_settings.base_url}/auth/logout",  # noqa: SLF001
                                           headers={"Authorization": f"Bearer {jwt}"},
                                           timeout=dealer_portal_settings.timeout_seconds)
            except Exception:  # noqa: BLE001
                pass
            if str(claims.get("dealer_id")) != str(dealer_id):
                out.append(f"{label}: login says dealer {claims.get('dealer_id')}, user list says {dealer_id} -- NOT linked")
                continue
            if "stock.upload" not in (claims.get("permissions") or []):
                out.append(f"{label}: dealer {dealer_id} has no stock-upload permission -- linked for PO-MST only")
                username = password = None
            if apply:
                done = link_vendor(vendor, dealer_id, session, username=username, password=password,
                                   dealer_name=claims.get("display_name") or vendor.name)
                out.append(f"{label}: dealer {dealer_id} ({claims.get('display_name')}) -- {'; '.join(done)}")
            else:
                out.append(f"{label}: would link to dealer {dealer_id} ({claims.get('display_name')}), login {username} checked OK")
        if apply:
            session.commit()
    return out


_reported: set[str] = set()


def backfill_job() -> None:
    """Scheduler, once a day: link any vendor the bot created whose dealer id
    could not be read at creation time (a slow Portal). A vendor that still
    needs a person is told to the admins once per bot run, not every day."""
    from backend.app.vendor_onboarding import portal_vendor_api as api

    if not api.settings.enabled:
        return
    lines = backfill(apply=True)
    for line in lines:
        logger.info("Vendor Portal link: %s", line)
    # Only vendors approved in the last 3 days: an old known case (VR-24) is
    # not repeated after every restart.
    from datetime import timedelta

    from sqlalchemy import select

    from backend.app.vendor_onboarding import models as m
    from core.db import get_session

    with get_session() as session:
        recent = {f"VR-{rid} " for rid in session.execute(
            select(m.VendorRegistrationRequest.id).where(
                m.VendorRegistrationRequest.decided_at >= now_ist_naive() - timedelta(days=3))
        ).scalars()}
    news = [line for line in lines if "by hand" in line and line not in _reported
            and any(line.startswith(code) for code in recent)]
    if news:
        _reported.update(news)
        from backend.app.vendor_onboarding import approvals
        from backend.app.vendor_onboarding.config import vendor_onboarding_settings

        approvals.notify(list(vendor_onboarding_settings.approver_numbers),
                         "Dealer Portal login save nahi ho paya -- inhe Dealer Portal page par jodna hoga:\n"
                         + "\n".join(f"• {line}" for line in news))
