"""Which Dealer Portal account does a ProcureHub vendor push into?

THE PROBLEM THIS SOLVES
-----------------------
ProcureHub holds `Bijvasan` and `Bijwasan` as two SEPARATE vendor rows --
they are the same real warehouse, spelled two ways. The Founder's decision
(31 Aug 2026) is that they STAY separate rows: no `merge_vendors.py` run, no
rows moved, allocation untouched.

But Dealer Portal has ONE dealer account for that warehouse. If each vendor
row pushed its own delta to it, the zeroing would be computed against half
the picture: `Bijvasan`'s push would zero parts that `Bijwasan` still
stocks, because `Bijvasan`'s own previous snapshot never contained them.

So the mapping is a GROUP, not a pair:

    one DP account  <--  many ProcureHub vendor rows

`delta.py` then computes the delta over the UNION of every vendor row in the
group, and ONE CSV is pushed for the account. The database is never touched.

CONFIGURING IT -- ENV ONLY
--------------------------
Two lines switch the integration on, then one block per dealer account::

    DEALER_PORTAL_ENABLED=true
    DEALER_PORTAL_ACCOUNTS=BIJWASAN,MANSAROVAR,MARUTI,FORD

Each key in `DEALER_PORTAL_ACCOUNTS` is the prefix for its own block. The
key is a label of your choosing (A-Z, 0-9, underscore) -- it never leaves
ProcureHub and does not have to match anything in DP::

    DEALER_PORTAL_BIJWASAN_USERNAME=bijwasan_dealer
    DEALER_PORTAL_BIJWASAN_PASSWORD=...
    DEALER_PORTAL_BIJWASAN_VENDORS=Bijvasan,Bijwasan,BIJWASHAN STOCK
    DEALER_PORTAL_BIJWASAN_FULL_SNAPSHOT=true

`_VENDORS` is the group -- comma-separated, and each entry is matched
against a ProcureHub vendor in either of two ways:

  1. EXACT `Vendor.vendor_code` match, case-insensitive (`NS_CT`, `MA_CT`).
  2. Tolerant NAME match -- case-insensitive, whitespace-tolerant, matched
     as a WHOLE WORD anywhere in the vendor's name. This is the identical
     rule `core.services.own_stock` already uses, so "Bijwasan" also matches
     "BIJWASAN HUB" and "Main Bijwasan Warehouse", but NOT "Bijwasannual
     Traders". A slight rename cannot silently drop a vendor out of its
     group.

Codes are tried first: an entry that matches some vendor's `vendor_code`
exactly is never also name-matched, so a code can never accidentally sweep
in an unrelated vendor.

OPTIONAL PER-ACCOUNT VARIABLES
------------------------------
    DEALER_PORTAL_<KEY>_TOKEN          permanent/refresh token -- when set,
                                       NO login call is made at all and
                                       USERNAME/PASSWORD are not required.
                                       This is the path to switch to once
                                       Vineet issues long-lived credentials.
    DEALER_PORTAL_<KEY>_DEVICE_ID      device id for the interactive login
                                       (defaults to a stable per-account
                                       value, which is what avoids the
                                       documented 409 SESSION_ALREADY_ACTIVE)
    DEALER_PORTAL_<KEY>_FULL_SNAPSHOT  default FALSE. Only true for vendors
                                       CONFIRMED to send a complete daily
                                       list. False = sold-out parts are
                                       computed and logged but NOT zeroed.
    DEALER_PORTAL_<KEY>_TAT_DAYS       overrides DEALER_PORTAL_TAT_DAYS
    DEALER_PORTAL_<KEY>_ENABLED        set false to park one account without
                                       removing its block

Credentials are read from the environment on every use, are never stored in
the database, and are never logged -- `DealerPortalAccount.__repr__` is
redacted deliberately.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from core.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class DealerPortalAccount:
    """One Dealer Portal dealer account and the ProcureHub vendor rows that
    feed it."""

    key: str
    username: str | None
    password: str | None
    token: str | None
    device_id: str
    vendor_patterns: list[str]
    full_snapshot: bool
    tat_days: int
    enabled: bool
    # Filled in by `resolve_account_vendors()` -- the Vendor.id values in
    # this group, which is what the delta union runs over.
    vendor_ids: list[int] = field(default_factory=list)
    vendor_names: list[str] = field(default_factory=list)

    @property
    def has_credentials(self) -> bool:
        return bool(self.token) or bool(self.username and self.password)

    @property
    def uses_permanent_token(self) -> bool:
        return bool(self.token)

    def __repr__(self) -> str:  # never leak secrets into a log or traceback
        return (
            f"DealerPortalAccount(key={self.key!r}, "
            f"vendors={self.vendor_patterns!r}, "
            f"full_snapshot={self.full_snapshot}, "
            f"auth={'token' if self.uses_permanent_token else 'login'})"
        )


def _env(key: str, suffix: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"DEALER_PORTAL_{key}_{suffix}")
    if value is None:
        return default
    value = value.strip()
    return value or default


def _int_env(key: str, suffix: str, fallback: int) -> int:
    raw = _env(key, suffix)
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "DEALER_PORTAL_%s_%s is not a whole number (%r) -- using %s.",
            key,
            suffix,
            raw,
            fallback,
        )
        return fallback


def _accounts_from_file() -> list[DealerPortalAccount]:
    """Accounts read from `DEALER_PORTAL_ACCOUNTS_FILE` -- one JSON file
    instead of three env lines per vendor.

        [
          {
            "key": "NORTHEND",
            "username": "northend_dealer",
            "password": "...",
            "vendors": ["Northend"],
            "full_snapshot": false
          },
          { "key": "ESSAAY", "token": "...", "vendors": ["EA_CT"] }
        ]

    `key`, `vendors`, and either `token` or `username`+`password` are
    required; `full_snapshot` (default false), `tat_days`, `device_id` and
    `enabled` are optional -- the same meanings as the env form.

    A missing or unreadable file is LOGGED AND IGNORED, never raised: a bad
    path must not stop the import that triggered the push. Nothing from the
    file is ever logged except the key and the vendor patterns."""
    path_text = dealer_portal_settings.accounts_file
    if not path_text:
        return []

    path = Path(path_text)
    if not path.exists():
        logger.warning(
            "DEALER_PORTAL_ACCOUNTS_FILE points at %s, which does not exist -- "
            "no accounts loaded from it.",
            path,
        )
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- a bad file must not reach the import
        logger.exception(
            "Could not read DEALER_PORTAL_ACCOUNTS_FILE (%s) -- no accounts "
            "loaded from it. Expected a JSON list of account objects.",
            path,
        )
        return []

    if isinstance(raw, dict):  # also accept {"NORTHEND": {...}} shape
        raw = [{**value, "key": key} for key, value in raw.items()]
    if not isinstance(raw, list):
        logger.error(
            "DEALER_PORTAL_ACCOUNTS_FILE (%s) must hold a JSON list of account "
            "objects -- got %s.",
            path,
            type(raw).__name__,
        )
        return []

    accounts: list[DealerPortalAccount] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning("Account #%d in %s is not an object -- skipped.", index, path)
            continue
        key = str(entry.get("key") or "").strip().upper()
        patterns = [str(v).strip() for v in (entry.get("vendors") or []) if str(v).strip()]
        if not key or not patterns:
            logger.warning(
                "Account #%d in %s needs both 'key' and a non-empty 'vendors' "
                "list -- skipped.",
                index,
                path,
            )
            continue

        account = DealerPortalAccount(
            key=key,
            username=(entry.get("username") or None),
            password=(entry.get("password") or None),
            token=(entry.get("token") or None),
            device_id=str(entry.get("device_id") or f"procurehub-{key.lower()}"),
            vendor_patterns=patterns,
            full_snapshot=bool(entry.get("full_snapshot", False)),
            tat_days=int(entry.get("tat_days") or dealer_portal_settings.tat_days),
            enabled=bool(entry.get("enabled", True)),
        )
        if not account.has_credentials:
            logger.warning(
                "Account %s in %s has neither 'token' nor 'username'+'password' "
                "-- skipped.",
                key,
                path,
            )
            continue
        accounts.append(account)

    if accounts:
        logger.info(
            "Dealer Portal: loaded %d account(s) from %s: %s",
            len(accounts),
            path,
            ", ".join(a.key for a in accounts),
        )
    return accounts


def load_accounts() -> list[DealerPortalAccount]:
    """Every configured account, in `DEALER_PORTAL_ACCOUNTS` order.

    A misconfigured block is LOGGED AND SKIPPED, never raised: a typo in one
    account must not stop the others -- and must never reach the import that
    triggered the push."""
    accounts: list[DealerPortalAccount] = []
    for key in dealer_portal_settings.account_keys:
        patterns = [
            part.strip()
            for part in (_env(key, "VENDORS", "") or "").split(",")
            if part.strip()
        ]
        if not patterns:
            logger.warning(
                "Dealer Portal account %s has no DEALER_PORTAL_%s_VENDORS -- skipped. "
                "List the ProcureHub vendor names or vendor codes that push into it.",
                key,
                key,
            )
            continue

        account = DealerPortalAccount(
            key=key,
            username=_env(key, "USERNAME"),
            password=_env(key, "PASSWORD"),
            token=_env(key, "TOKEN"),
            device_id=_env(key, "DEVICE_ID", f"procurehub-{key.lower()}") or "",
            vendor_patterns=patterns,
            full_snapshot=(_env(key, "FULL_SNAPSHOT", "false") or "false").lower() == "true",
            tat_days=_int_env(key, "TAT_DAYS", dealer_portal_settings.tat_days),
            enabled=(_env(key, "ENABLED", "true") or "true").lower() == "true",
        )
        if not account.has_credentials:
            logger.warning(
                "Dealer Portal account %s has neither DEALER_PORTAL_%s_TOKEN nor "
                "DEALER_PORTAL_%s_USERNAME/_PASSWORD -- skipped.",
                key,
                key,
                key,
            )
            continue
        accounts.append(account)

    # File-defined accounts come in behind the env ones: an account key
    # present in BOTH is taken from the env, so a single vendor can always be
    # overridden without editing the credentials file.
    by_key = {a.key for a in accounts}
    for account in _accounts_from_file():
        if account.key in by_key:
            logger.info(
                "Dealer Portal account %s is defined in BOTH the env and the "
                "accounts file -- using the env one.",
                account.key,
            )
            continue
        accounts.append(account)
    return accounts


def matches_name(pattern: str, vendor_name: str | None) -> bool:
    """Tolerant whole-word name match -- the identical rule
    `core.services.own_stock.is_own_stock_vendor` uses, so the two stay
    consistent for the Bijwasan / Mansarovar spelling pairs."""
    if not vendor_name:
        return False
    name = vendor_name.strip().casefold()
    target = pattern.strip().casefold()
    if not target:
        return False
    if name == target:
        return True
    return re.search(rf"(?<![0-9a-z]){re.escape(target)}(?![0-9a-z])", name) is not None


def resolve_account_vendors(account: DealerPortalAccount, session) -> DealerPortalAccount:
    """Populate `account.vendor_ids` with every ProcureHub vendor row in this
    account's group. Returns the same account, mutated."""
    from core.models import Vendor
    from core.services.own_stock import is_own_stock_vendor

    vendors = session.query(Vendor).all()
    by_code = {
        (v.vendor_code or "").strip().casefold(): v for v in vendors if v.vendor_code
    }

    matched: dict[int, str] = {}
    for pattern in account.vendor_patterns:
        code_hit = by_code.get(pattern.strip().casefold())
        if code_hit is not None:
            matched[code_hit.id] = code_hit.name
            continue
        for vendor in vendors:
            if matches_name(pattern, vendor.name):
                matched[vendor.id] = vendor.name

    # OWN-STOCK GUARD (Harun + Founder, call of 4 Sep 2026). Bijwasan,
    # Mansarovar and Jaipur stock is exported FROM Dealer Portal's ERP before
    # it ever reaches WhatsApp, so pushing it back would double it inside DP.
    # Dropped here even if someone lists the name in a group by mistake.
    excluded: list[str] = []
    for vendor_id in list(matched):
        name = matched[vendor_id]
        vendor = session.get(Vendor, vendor_id)
        is_own = is_own_stock_vendor(
            name, flag=bool(getattr(vendor, "is_own_stock", False))
        )
        blocked = any(
            matches_name(pattern, name)
            for pattern in dealer_portal_settings.exclude_vendors
        )
        if is_own or blocked:
            excluded.append(name)
            del matched[vendor_id]

    if excluded:
        logger.warning(
            "Dealer Portal %s: NOT pushing %s -- this stock comes OUT of DP's own "
            "ERP, so pushing it back would double it. Remove the name from "
            "DEALER_PORTAL_%s_VENDORS to silence this.",
            account.key,
            ", ".join(sorted(excluded)),
            account.key,
        )

    account.vendor_ids = sorted(matched)
    account.vendor_names = [matched[vid] for vid in account.vendor_ids]
    if not account.vendor_ids:
        logger.warning(
            "Dealer Portal account %s matched NO ProcureHub vendor for %s -- "
            "nothing will be pushed for it.",
            account.key,
            account.vendor_patterns,
        )
    else:
        logger.info(
            "Dealer Portal account %s covers %d vendor row(s): %s",
            account.key,
            len(account.vendor_ids),
            ", ".join(account.vendor_names),
        )
    return account


def account_for_vendor(vendor_id: int, session) -> DealerPortalAccount | None:
    """The account a given vendor pushes into, with its full group resolved,
    or None if that vendor is not mapped to any account.

    A vendor listed under two accounts is a configuration error; the FIRST
    account in `DEALER_PORTAL_ACCOUNTS` order wins and the clash is logged."""
    hits: list[DealerPortalAccount] = []
    for account in load_accounts():
        if not account.enabled:
            continue
        resolve_account_vendors(account, session)
        if vendor_id in account.vendor_ids:
            hits.append(account)

    if not hits:
        return None
    if len(hits) > 1:
        logger.error(
            "Vendor %s is mapped to several Dealer Portal accounts (%s). Using %s. "
            "Fix DEALER_PORTAL_<KEY>_VENDORS so each vendor belongs to exactly one.",
            vendor_id,
            ", ".join(a.key for a in hits),
            hits[0].key,
        )
    return hits[0]


def account_by_key(key: str, session) -> DealerPortalAccount | None:
    """One account by its `DEALER_PORTAL_ACCOUNTS` key, group resolved --
    used by `dealer_portal_resync.py`."""
    wanted = key.strip().upper()
    for account in load_accounts():
        if account.key == wanted:
            return resolve_account_vendors(account, session)
    return None
