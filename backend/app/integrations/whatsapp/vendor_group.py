"""A WhatsApp group for every NEW vendor (team, 29 Oct 2026).

When a vendor registration is created, the bot (through Whapi, on Alam sir's
number) makes "CarTrends 🤝 <Vendor>" with the vendor's WhatsApp number and
WHAPI_NEW_VENDOR_GROUP_MEMBERS (Seema, Rajkumar), saves it as that vendor's
group -- so the daily "stock share kar dijiye" reaches it and stock posted
there is uploaded -- and says hello in it. A vendor whose privacy setting
stops him being added gets the group's invite link on WhatsApp instead.

Off unless WHAPI_AUTO_VENDOR_GROUP=true. Runs in its own thread and never
raises: a vendor is created whether or not the group could be made."""

from __future__ import annotations

import os
import threading

from core.logging_setup import get_logger

logger = get_logger(__name__)

WELCOME = "Namaste ji 🙏 CarTrends mein aapka swagat hai. Daily stock list isi group mein share kar dijiye."


def enabled() -> bool:
    from backend.app.integrations.whatsapp import whapi

    return (os.environ.get("WHAPI_AUTO_VENDOR_GROUP") or "false").strip().lower() == "true" and whapi.enabled()


def _team() -> list[str]:
    from backend.app.integrations.whatsapp.registry import normalize_number

    raw = os.environ.get("WHAPI_NEW_VENDOR_GROUP_MEMBERS") or "Seema:9289015770,Rajkumar:9217396458"
    return [n for n in (normalize_number(x.split(":")[-1].strip()) for x in raw.split(",") if x.strip()) if n]


def group_name(vendor_name: str) -> str:
    return f"CarTrends 🤝 {' '.join(vendor_name.split())}"[:100]  # WhatsApp's limit


def create_for_vendor(vendor_code: str, vendor_name: str, vendor_number: str) -> str | None:
    """Create and save the group; the new group id, or None. Never raises."""
    from backend.app.integrations.whatsapp import whapi
    from backend.app.integrations.whatsapp.models import WhapiVendorGroup
    from backend.app.integrations.whatsapp.registry import normalize_number
    from core.db import get_session

    try:
        vendor = normalize_number(vendor_number)
        if not (enabled() and vendor_code and vendor):
            return None
        if vendor_code.upper() in set(whapi.vendor_group_codes().values()):
            logger.info("Vendor group: %s already has a group -- none created.", vendor_code)
            return None
        created = whapi._request("POST", "/groups", json={"subject": group_name(vendor_name), "participants": [vendor, *_team()]}).json()
        group = created.get("group_id") or created.get("id")
        if not group:
            logger.error("Vendor group: Whapi made no group for %s: %s", vendor_code, str(created)[:200])
            return None
        with get_session() as session:
            session.merge(WhapiVendorGroup(group_id=group, vendor_code=vendor_code.upper(), name=group_name(vendor_name), created_by_bot=True))
        whapi.forget_cached_groups()
        whapi.record(group, "group created", None, f"{group_name(vendor_name)} ({vendor_code})")
        members = {str(p.get("id", "")).split("@")[0] for p in created.get("participants") or []}
        whapi.post_as_owner(group, WELCOME)
        if vendor not in members:
            _send_invite(group, vendor, vendor_name)
        logger.info("Vendor group %s created for %s (%s).", group, vendor_name, vendor_code)
        return group
    except Exception:  # noqa: BLE001 -- the vendor is created either way
        logger.exception("Vendor group: could not create one for %s.", vendor_code)
        return None


def _send_invite(group: str, vendor: str, vendor_name: str) -> None:
    """WhatsApp would not add him (privacy setting): send the join link."""
    from backend.app.integrations.whatsapp import whapi
    from backend.app.integrations.whatsapp.outbound import send_reply_safe

    code = (whapi._request("GET", f"/groups/{group}/invite").json() or {}).get("invite_code")
    if code:
        with whapi.direct_only():
            send_reply_safe(vendor, f"Namaste ji 🙏 {vendor_name} ke liye CarTrends group: https://chat.whatsapp.com/{code}\nDaily stock isi group mein share kar dijiye.")


def create_for_vendor_in_background(vendor_code: str, vendor_name: str, vendor_number: str) -> None:
    if enabled():
        threading.Thread(target=create_for_vendor, args=(vendor_code, vendor_name, vendor_number), daemon=True).start()
