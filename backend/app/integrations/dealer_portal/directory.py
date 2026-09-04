"""The Dealer Portal dealer roster -- used to show the admin the EXACT names
and ids to choose between.

Founder, 4 Sep 2026: when ProcureHub cannot tell which DP account a vendor's
stock belongs to, it must ask once, showing the real candidates rather than
guessing. This module fetches that list.

TWO SAFETY RULES, BOTH DELIBERATE
---------------------------------
1. **Only id / name / type are kept.** DP's `GET /dealers/` currently returns
   every dealer record INCLUDING a cleartext `password` field, to any
   authenticated dealer account. That is DP's bug, reported separately -- but
   ProcureHub must not become a second copy of it, so everything except the
   three fields below is dropped the moment the response is parsed. Nothing
   from it is written to disk or logged.

2. **Cached in memory, briefly.** 8,000+ dealers should not be re-fetched for
   every question. The cache is per-process and expires, so a dealer added on
   DP shows up without a restart.

`dealer_type` is DP's own classification and is the reliable way to tell a
CarTrends warehouse from an external vendor -- far better than matching
names. Of 8,326 dealers only 7 are `cartrend_dealer`, and those are exactly
the own-stock warehouses whose stock must never be pushed back.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

# DP's own classification for a CarTrends-owned warehouse.
OWN_WAREHOUSE_TYPE = "cartrend_dealer"

_CACHE: list["DealerRecord"] = []
_CACHE_AT: float = 0.0
_CACHE_TTL_SECONDS = 900  # 15 minutes


@dataclass(frozen=True)
class DealerRecord:
    """A DP dealer, reduced to what ProcureHub is allowed to hold."""

    dealer_id: int
    name: str
    dealer_type: str | None

    @property
    def is_own_warehouse(self) -> bool:
        """True for a CarTrends warehouse -- stock flows OUT of these, so
        pushing to one would double the stock DP already holds."""
        return (self.dealer_type or "").strip().lower() == OWN_WAREHOUSE_TYPE


def _fetch(account) -> list[DealerRecord]:
    """Read the roster using any configured account's credentials."""
    from backend.app.integrations.dealer_portal import client

    token = client.token_for(account)
    requests = client._requests()  # noqa: SLF001 -- same package, one HTTP layer
    response = requests.get(
        f"{dealer_portal_settings.base_url}/dealers/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=dealer_portal_settings.timeout_seconds,
        allow_redirects=True,
    )
    if response.status_code != 200:
        raise client.DealerPortalError(
            f"Dealer roster request failed: HTTP {response.status_code}."
        )

    records: list[DealerRecord] = []
    for row in response.json() or []:
        if not isinstance(row, dict):
            continue
        dealer_id = row.get("dealer_id")
        if dealer_id is None:
            continue
        # ONLY these three fields. Everything else -- including the password
        # DP wrongly returns -- is discarded here and never stored.
        records.append(
            DealerRecord(
                dealer_id=int(dealer_id),
                name=str(row.get("dealer_name") or "").strip(),
                dealer_type=(row.get("dealer_type") or None),
            )
        )
    return records


def load(account, *, force: bool = False) -> list[DealerRecord]:
    """The roster, from cache when fresh. Returns [] on any failure -- the
    caller falls back to asking the admin for a dealer id directly."""
    global _CACHE, _CACHE_AT

    if not force and _CACHE and (time.time() - _CACHE_AT) < _CACHE_TTL_SECONDS:
        return _CACHE
    try:
        records = _fetch(account)
    except Exception:  # noqa: BLE001 -- never reach the import that triggered this
        logger.exception("Could not read the Dealer Portal dealer roster.")
        return _CACHE  # a stale list still beats none
    _CACHE = records
    _CACHE_AT = time.time()
    logger.info(
        "Dealer Portal roster loaded: %d dealer(s), %d CarTrends warehouse(s).",
        len(records),
        sum(1 for r in records if r.is_own_warehouse),
    )
    return _CACHE


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 1}


def search(vendor_name: str, records: list[DealerRecord], limit: int = 6) -> list[DealerRecord]:
    """Candidate dealers for a ProcureHub vendor name, best first.

    Scored on shared words rather than a substring, so "A K Motors" finds
    "A K Motors Karol Bagh" and "A. K. Motors Fbd" alike. Deliberately
    returns SEVERAL -- the whole point is to let a human choose, not to pick
    for them."""
    wanted = _tokens(vendor_name)
    if not wanted:
        return []

    scored: list[tuple[float, DealerRecord]] = []
    for record in records:
        have = _tokens(record.name)
        if not have:
            continue
        shared = wanted & have
        if not shared:
            continue
        # Favour covering the vendor's words, then a tight dealer name.
        score = len(shared) / len(wanted) + len(shared) / len(have) * 0.25
        scored.append((score, record))

    scored.sort(key=lambda pair: (-pair[0], pair[1].dealer_id))
    return [record for _score, record in scored[:limit]]
