"""Who is already on the Dealer Portal, by MOBILE and EMAIL (21 Nov 2026) --
for the vendor form, so a duplicate is refused while the agent types.

Built from the two lists the bot already reads with the admin login:
    GET /users/?limit=10000   every login: phone, email, display name, dealer id
    GET /dealers/             every dealer: dealer_contact (phone), dealer name
Both are slow (thousands of rows), so they are read every 6 hours by the
scheduler (job `portal_directory_refresh`) and kept in memory. The form
never waits for the Portal: before the first read, or when the Portal is
down, the Portal check is skipped (the bot's own registrations are still
checked, and the final duplicate check at approval reads the Portal fresh).

The Portal's lists carry NO GST number, so a GSTIN cannot be checked here
(asked of the Portal team). Nothing is written to disk; only who owns which
phone / email is kept, never a password."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from core.logging_setup import get_logger

logger = get_logger(__name__)

REFRESH_SECONDS = 6 * 3600


@dataclass
class Directory:
    phones: dict[str, str] = field(default_factory=dict)  # 10 digits -> who
    emails: dict[str, str] = field(default_factory=dict)  # lower-case email -> who
    loaded_at: float = 0.0


_lock = threading.Lock()
_current = Directory()


_MOBILE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?([6-9]\d{4}[\s-]?\d{5})(?!\d)")


def _ten_digit_numbers(value) -> list[str]:
    """Every Indian mobile inside a contact field ('+91 98110 12345, 9810...')."""
    return [re.sub(r"\D", "", m) for m in _MOBILE.findall(str(value or ""))]


def _who(name, kind: str, dealer_id) -> str:
    label = str(name or "").strip() or "unknown"
    return f"{label} ({kind}{f', dealer {dealer_id}' if dealer_id else ''})"


def build(users: list[dict], dealers: list[dict]) -> Directory:
    directory = Directory(loaded_at=time.monotonic())
    for row in dealers:
        who = _who(row.get("dealer_name"), "dealer", row.get("dealer_id"))
        for phone in _ten_digit_numbers(row.get("dealer_contact")):
            directory.phones.setdefault(phone, who)
    for row in users:
        who = _who(row.get("display_name") or row.get("username"), "login", row.get("dealer_id"))
        for phone in _ten_digit_numbers(row.get("phone")):
            directory.phones.setdefault(phone, who)
        email = str(row.get("email") or "").strip().lower()
        if "@" in email:
            directory.emails.setdefault(email, who)
    return directory


def refresh() -> bool:
    """Read both Portal lists again. True when it worked. Never raises."""
    from backend.app.vendor_onboarding import portal_vendor_api as api

    global _current
    if not api.settings.enabled:
        return False
    try:
        token = api.login()
        try:
            users = api._get_rows(token, "/users/?limit=10000")  # noqa: SLF001
            dealers = api._get_rows(token, "/dealers/")  # noqa: SLF001
        finally:
            api.logout(token)
    except Exception:  # noqa: BLE001 -- keep the old copy; try again next time
        logger.exception("Portal directory: could not read the dealer / login lists.")
        return False
    fresh = build(users, dealers)
    with _lock:
        _current = fresh
    logger.info("Portal directory refreshed: %d phones, %d emails.", len(fresh.phones), len(fresh.emails))
    return True


def refresh_job() -> None:
    """Scheduler: every 6 hours (and once shortly after start)."""
    refresh()


def current() -> Directory | None:
    """The last copy read, or None before the first read."""
    with _lock:
        return _current if _current.loaded_at else None


def phone_owner(mobile: str) -> str | None:
    directory = current()
    if directory is None:
        return None
    digits = re.sub(r"\D", "", str(mobile or ""))[-10:]
    return directory.phones.get(digits) if len(digits) == 10 else None


def email_owner(email: str) -> str | None:
    directory = current()
    if directory is None:
        return None
    return directory.emails.get(str(email or "").strip().lower())
