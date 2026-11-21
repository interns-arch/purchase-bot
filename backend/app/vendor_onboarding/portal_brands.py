"""A new vendor's brands -> the Dealer Portal's brand MAPPING and DISCOUNT, and
the bot's own brand list (21 Nov 2026; the Portal team's guide
THIRD_PARTY_VENDOR_BRAND_MAPPING_INTEGRATION.md).

Two separate Portal records, in this order:

  1. MAPPING   POST /masterpurchase/vendor-mappings/upload        CSV "Dealer Name,Brand"
               who appears for a brand -- decides the Portal's vendor options
               and so whether a PO-MST can be made for him.
  2. DISCOUNT  POST /masterpurchase/vendor-brand-discounts/upload  CSV "Vendor Name,Brand,Discount%"
               only when the vendor gave one. A discount alone never maps.

Both are checked by reading the tables back. The brand must be the Portal's
EXACT master brand (GET /parts/brands): "HARISONS", not "HARISON". What the
vendor typed is matched to it (`match_brand`); a brand with no match is
reported, never invented.

Then the same brands go on the bot's own vendor_brands list (who is asked for
which brand), at the END of the brand's order -- moved up on the Vendors page.

Never raises: a vendor is created even if this step fails; what failed is
returned in words for the approver's message and the log."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.logging_setup import get_logger

logger = get_logger(__name__)


# ====================================================================== brand names
def _key(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def split_brands(text: str | None) -> list[str]:
    """'Maruti, Hyundai & Bosch' / 'ford' / 'Maruti and Tata' -> names."""
    parts = re.split(r"[,;/&\n]+|\band\b", str(text or ""), flags=re.I)
    out: list[str] = []
    for part in parts:
        name = " ".join(part.split()).strip(" .-")
        if name and _key(name) and name.upper() not in {n.upper() for n in out}:
            out.append(name)
    return out


def match_brand(raw: str, master: list[str]) -> str | None:
    """The Portal's exact master brand for what the vendor typed, or None.
    Exact (ignoring case / spaces / dots) -> the bot's known aliases
    ('maruti' -> 'MARUTI SUZUKI') -> singular/plural ('harison' -> 'HARISONS')."""
    from backend.app.advance_orders.brands import canonical_brand

    by_key = {_key(b): b for b in master if _key(b)}
    tries = [raw, canonical_brand(raw) or ""]
    for name in tries:
        k = _key(name)
        if k in by_key:
            return by_key[k]
    for name in tries:
        k = _key(name)
        if not k:
            continue
        plural = [b for kk, b in by_key.items() if kk in (k + "S", k + "ES") or (k.endswith("S") and kk == k[:-1])]
        if len(plural) == 1:
            return plural[0]
    return None


@dataclass
class BrandPlan:
    brands: list[tuple[str, str | None, Decimal | None]] = field(default_factory=list)  # (typed, master or None, discount)

    @property
    def matched(self) -> list[tuple[str, Decimal | None]]:
        return [(m, d) for _t, m, d in self.brands if m]

    @property
    def unmatched(self) -> list[str]:
        return [t for t, m, _d in self.brands if not m]


def plan_brands(answers: dict, master: list[str]) -> BrandPlan:
    """Every brand from "Brands supplied" and "Brand-wise discount", matched to
    the Portal master, with the discount when he gave one."""
    discounts: dict[str, Decimal] = {}
    typed: list[str] = []
    for row in answers.get("brand_discounts") or []:
        name = str(row.get("brand") or "").strip()
        if not name:
            continue
        typed.append(name)
        try:
            discounts[_key(name)] = Decimal(str(row.get("discount_pct")))
        except (InvalidOperation, ValueError):
            pass
    for name in split_brands(answers.get("brands_supplied")):
        if _key(name) not in {_key(t) for t in typed}:
            typed.append(name)
    plan = BrandPlan()
    seen: set[str] = set()
    for name in typed:
        master_name = match_brand(name, master)
        k = _key(master_name or name)
        if k in seen:
            continue
        seen.add(k)
        plan.brands.append((name, master_name, discounts.get(_key(name))))
    return plan


# ====================================================================== Portal calls
def _csv(header: list[str], rows: list[list]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode()


def _get(token: str, path: str):
    from backend.app.vendor_onboarding import portal_vendor_api as api

    response = httpx.get(api._url(path), headers=api._headers(token), timeout=api.settings.list_timeout)  # noqa: SLF001
    response.raise_for_status()
    return response.json()


def _upload(token: str, path: str, name: str, content: bytes) -> dict:
    from backend.app.vendor_onboarding import portal_vendor_api as api

    headers = {k: v for k, v in api._headers(token).items() if k.lower() != "content-type"}  # noqa: SLF001
    response = httpx.post(api._url(path), headers=headers, files={"file": (name, content, "text/csv")},  # noqa: SLF001
                          timeout=api.settings.timeout)
    try:
        body = response.json()
    except ValueError:
        body = {"detail": (response.text or "")[:300]}
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} {str(body.get('detail') if isinstance(body, dict) else body)[:200]}")
    return body if isinstance(body, dict) else {}


def master_brands(token: str) -> list[str]:
    rows = _get(token, "/parts/brands")
    return [str(b).strip() for b in (rows if isinstance(rows, list) else []) if str(b).strip()]


def portal_mappings(token: str, dealer_id: int) -> set[str]:
    rows = _get(token, "/masterpurchase/vendor-mappings")
    rows = rows if isinstance(rows, list) else (rows.get("items") or rows.get("data") or [])
    return {_key(r.get("brand")) for r in rows if str(r.get("dealer_id")) == str(dealer_id)}


def portal_discounts(token: str, dealer_id: int) -> dict[str, float]:
    rows = _get(token, "/masterpurchase/vendor-brand-discounts")
    rows = rows if isinstance(rows, list) else (rows.get("items") or rows.get("data") or [])
    return {_key(r.get("brand")): float(r.get("discount_percent") or 0) for r in rows if str(r.get("dealer_id")) == str(dealer_id)}


def map_on_portal(token: str, dealer_id: int, dealer_name: str, plan: BrandPlan, *, apply: bool = True) -> list[str]:
    """Mapping first, then discounts; each checked by reading back. Returns
    what happened, one line per brand. Brands already mapped / already at the
    same discount are left alone."""
    out: list[str] = []
    if not plan.matched:
        return out
    have_map = portal_mappings(token, dealer_id)
    to_map = [m for m, _d in plan.matched if _key(m) not in have_map]
    for m, _d in plan.matched:
        if _key(m) in have_map:
            out.append(f"{m}: already mapped")
    if to_map:
        if apply:
            body = _upload(token, "/masterpurchase/vendor-mappings/upload", "vendor_mapping.csv",
                           _csv(["Dealer Name", "Brand"], [[dealer_name, m] for m in to_map]))
            errors = body.get("errors") or []
            now = portal_mappings(token, dealer_id)
            for m in to_map:
                if _key(m) in now:
                    out.append(f"{m}: mapped")
                else:
                    why = next((str(e) for e in errors if _key(m) in _key(e)), None) or (str(errors[0]) if errors else "not in the list after upload")
                    out.append(f"{m}: NOT mapped ({why[:120]})")
        else:
            out.extend(f"{m}: would map" for m in to_map)
    with_disc = [(m, d) for m, d in plan.matched if d is not None]
    if with_disc:
        have_disc = portal_discounts(token, dealer_id)
        to_set = [(m, d) for m, d in with_disc if abs(have_disc.get(_key(m), -1) - float(d)) > 0.001]
        for m, d in with_disc:
            if (m, d) not in to_set:
                out.append(f"{m}: discount {d:g}% already set")
        if to_set:
            if apply:
                body = _upload(token, "/masterpurchase/vendor-brand-discounts/upload", "vendor_discount.csv",
                               _csv(["Vendor Name", "Brand", "Discount%"], [[dealer_name, m, f"{d:g}"] for m, d in to_set]))
                now = portal_discounts(token, dealer_id)
                for m, d in to_set:
                    ok = abs(now.get(_key(m), -1) - float(d)) <= 0.001
                    failed = body.get("failed_rows") or []
                    out.append(f"{m}: discount {d:g}% set" if ok else f"{m}: discount NOT set ({str(failed[:1])[:120]})")
            else:
                out.extend(f"{m}: would set discount {d:g}%" for m, d in to_set)
    return out


# ====================================================================== the bot's own list
def save_bot_brands(vendor, plan: BrandPlan, session: Session, answers: dict | None = None) -> list[str]:
    """The same brands on vendor_brands (who is asked for which brand). Portal
    master names when matched, else what he typed (canonical). A known
    discount is a standing %; none = he is asked each time. New rows go to the
    END of the brand's order. Existing rows keep their place."""
    from backend.app.advance_orders.brands import canonical_brand
    from backend.app.advance_orders.models import DISC_PERCENT, DISC_RATE, VendorBrand

    answers = answers or {}
    transport = " / ".join(x for x in (answers.get("dispatch_mode"), answers.get("freight_terms")) if x) or None
    out: list[str] = []
    for typed, master_name, discount in plan.brands:
        brand = canonical_brand(master_name or typed)
        if not brand:
            continue
        row = session.execute(
            select(VendorBrand).where(VendorBrand.brand == brand, VendorBrand.vendor_id == vendor.id)
        ).scalar_one_or_none()
        if row is None:
            last = session.execute(select(func.max(VendorBrand.priority)).where(VendorBrand.brand == brand)).scalar()
            row = VendorBrand(brand=brand, vendor_id=vendor.id, priority=(last or 0) + 1,
                              discount_note="vendor registration")
            session.add(row)
            out.append(f"{brand}: on the bot's list")
        row.active = True
        if discount is not None:
            row.discount_type, row.discount_pct = DISC_PERCENT, discount
        elif row.discount_pct is None:
            row.discount_type = DISC_RATE  # asked each time
        if transport:
            row.transport = transport
    session.flush()
    return out


# ====================================================================== the whole step
def apply_for_vendor(vendor, answers: dict, dealer_id: int | None, dealer_name: str | None,
                     session: Session, *, apply: bool = True, token: str | None = None,
                     master: list[str] | None = None) -> list[str]:
    """Portal mapping + discount (when the dealer id is known), then the bot's
    list. One line per thing done / not done. Never raises."""
    from backend.app.vendor_onboarding import portal_vendor_api as api

    lines: list[str] = []
    own = token is None  # log in (and out) only when no session was handed in
    master = list(master or [])
    try:
        if own and api.settings.enabled:
            token = api.login()
        if token and not master:
            master = master_brands(token)
    except Exception as exc:  # noqa: BLE001 -- the bot's list is still saved below
        lines.append(f"Portal brand list not read ({str(exc)[:80]})")
    try:
        plan = plan_brands(answers, master)
        for typed in plan.unmatched if master else []:
            lines.append(f"{typed}: not a Portal brand -- not mapped")
        if token and dealer_id and dealer_name and plan.matched:
            try:
                lines.extend(map_on_portal(token, dealer_id, dealer_name, plan, apply=apply))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Vendor %s: Portal brand mapping failed.", vendor.id)
                lines.append(f"Portal mapping failed ({str(exc)[:100]})")
        # With the Portal's list known, only real Portal brands go on the bot's
        # list ("PRINTING SOLUTION" would never get a Portal part anyway).
        bot_plan = BrandPlan([b for b in plan.brands if b[1]]) if master else plan
        if apply:
            lines.extend(save_bot_brands(vendor, bot_plan, session, answers))
        else:
            lines.extend(f"{(m or t)}: would go on the bot's list" for t, m, _d in bot_plan.brands)
    finally:
        if own and token:
            api.logout(token)
    logger.info("Vendor %s (%s) brands: %s", vendor.id, vendor.name, "; ".join(lines) or "none")
    return lines


# ====================================================================== backfill
def backfill(*, apply: bool) -> list[str]:
    """Every CREATED registration whose vendor is linked to a Portal dealer:
    its form brands -> Portal mapping + discount + the bot's list. The dealer
    name sent is the Portal's own (GET /dealers/{id}). Dry run unless apply."""
    from backend.app.integrations.dealer_portal import mapping
    from backend.app.vendor_onboarding import models as m
    from backend.app.vendor_onboarding import portal_vendor_api as api
    from core.db import get_session
    from core.models import Vendor

    out: list[str] = []
    with get_session() as session:
        requests = session.execute(
            select(m.VendorRegistrationRequest).where(m.VendorRegistrationRequest.status == m.REG_CREATED)
            .order_by(m.VendorRegistrationRequest.id)
        ).scalars().all()
        token = api.login()
        try:
            master = master_brands(token)
            for request in requests:
                vendor = session.get(Vendor, request.vendor_id) if request.vendor_id else None
                if vendor is None:
                    continue
                label = f"VR-{request.id} {vendor.name}"
                row = mapping.get_map(vendor.id, session)
                if not mapping.is_resolved(row):
                    out.append(f"{label}: not linked to a Portal dealer -- skipped")
                    continue
                try:
                    dealer = _get(token, f"/dealers/{int(row.dp_dealer_id)}")
                    dealer_name = str(dealer.get("dealer_name") or "").strip()
                except Exception as exc:  # noqa: BLE001
                    out.append(f"{label}: dealer {row.dp_dealer_id} not read ({str(exc)[:60]}) -- skipped")
                    continue
                lines = apply_for_vendor(vendor, request.answers or {}, int(row.dp_dealer_id), dealer_name, session,
                                         apply=apply, token=token, master=master)
                out.append(f"{label} (dealer {row.dp_dealer_id} '{dealer_name}'): " + ("; ".join(lines) or "no brands on the form"))
        finally:
            api.logout(token)
        if apply:
            session.commit()
    return out


# ====================================================================== while the form is filled
_master_cache: tuple[float, list[str]] | None = None
_MASTER_SECONDS = 6 * 3600


def cached_master() -> list[str]:
    """The Portal's brand list, read at most every 6 hours. [] when the Portal
    cannot be read -- the form then takes the brands as typed, as before."""
    import time

    from backend.app.vendor_onboarding import portal_vendor_api as api

    global _master_cache
    now = time.monotonic()
    if _master_cache and now - _master_cache[0] < _MASTER_SECONDS:
        return _master_cache[1]
    if not api.settings.enabled:
        return []
    try:
        token = api.login()
        try:
            master = master_brands(token)
        finally:
            api.logout(token)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read the Portal brand list.")
        return _master_cache[1] if _master_cache else []
    _master_cache = (now, master)
    return master


def closest_brand(raw: str, master: list[str]) -> str | None:
    """A misspelt brand -> the Portal brand it most likely means ('mahindr' ->
    'MAHINDRA'), else None. Known aliases and plurals first, then spelling."""
    import difflib

    found = match_brand(raw, master)
    if found:
        return found
    by_key = {_key(b): b for b in master if _key(b)}
    close = difflib.get_close_matches(_key(raw), list(by_key), n=1, cutoff=0.8)
    return by_key[close[0]] if close else None


def check_typed(text: str, master: list[str]) -> tuple[str, list[tuple[str, str]], list[str]]:
    """(the brands as the Portal names them, [(typed, Portal name) that differ],
    [typed with no Portal brand]). An exact name (any case) is not a change."""
    names: list[str] = []
    changes: list[tuple[str, str]] = []
    unknown: list[str] = []
    for typed in split_brands(text):
        found = closest_brand(typed, master)
        if found is None:
            unknown.append(typed)
            names.append(typed.upper())
        else:
            if _key(found) != _key(typed):
                changes.append((typed, found))
            if found not in names:
                names.append(found)
    return ", ".join(names), changes, unknown
