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
