"""Bot vendor -> Portal dealer links, for PO-MST creation only.

Rules (user, 12 Nov 2026):
 1. An existing link always wins and is never overwritten by a suggestion.
    "Existing" = an admin-confirmed `dealer_portal_vendor_map` row (the stock
    upload map -- read here, NEVER written) or an earlier row in
    `portal_vendor_links`.
 2. (e.g. vendor 56 Ark Vehicles stays on dealer 1709, never the suggested 14.)
 3. Keys are the immutable Bot Vendor ID and Portal Dealer ID -- never names.
 4. Y accepts the suggested dealer only for an UNLINKED vendor.
 5. A typed dealer ID must exist in `portal_dealers` (dealers the Portal
    itself has offered as vendor options).
 6. Blank = no change.
 7. N = onboarding required; it never deletes an existing link.
 8. `plan()` is the dry run; `apply()` commits exactly that plan.
 9. Idempotent: running the same file twice changes nothing the second time.
    Stock uploads (`dealer_portal_vendor_map`) and history are never touched.

`accept_exact=True` additionally accepts a LIKELY row with no answer when the
vendor's name and the dealer's Portal name are IDENTICAL (letters/digits),
no other Portal dealer has that same name, and the vendor is unlinked.
CHECK / NO MATCH rows are never accepted automatically."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.portal_procurement import models as m
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

KEEP = "keep_existing"
LINK = "link"
ALREADY = "already_linked"
ONBOARD = "onboarding_required"
NOCHANGE = "no_change"
REJECT = "rejected"
CONFLICT = "conflict_not_changed"


def norm_name(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def existing(vendor_id: int, session: Session) -> tuple[int | None, str | None]:
    """(dealer_id, where) of the link that already exists for this vendor."""
    from core.models import DealerPortalVendorMap, DealerPortalVendorMapStatus

    mp = session.execute(
        select(DealerPortalVendorMap).where(DealerPortalVendorMap.vendor_id == vendor_id)
    ).scalar_one_or_none()
    if mp is not None and mp.status == DealerPortalVendorMapStatus.CONFIRMED and mp.dp_dealer_id:
        return int(mp.dp_dealer_id), "stock-upload map (admin confirmed)"
    link = session.execute(select(m.PortalVendorLink).where(m.PortalVendorLink.vendor_id == vendor_id)).scalar_one_or_none()
    if link is not None and link.status == m.LINK_LINKED and link.dealer_id:
        return int(link.dealer_id), f"procurement link ({link.source or '-'})"
    return None, None


def resolve(vendor_id: int | None, session: Session) -> int | None:
    if not vendor_id:
        return None
    return existing(vendor_id, session)[0]


def refresh_dealers(session: Session, rows: list[dict], brand: str | None = None) -> int:
    """Store dealers from a vendor-options response (`/active/{id}/vendors`)."""
    n = 0
    for row in rows:
        try:
            dealer_id = int(row.get("dealer_id"))
        except (TypeError, ValueError):
            continue
        dealer = session.get(m.PortalDealer, dealer_id)
        if dealer is None:
            dealer = m.PortalDealer(dealer_id=dealer_id, brands=[])
            session.add(dealer)
            n += 1
        dealer.dealer_name = row.get("dealer_name") or dealer.dealer_name
        if row.get("odoo_partner_id") is not None:
            dealer.odoo_partner_id = row.get("odoo_partner_id")
        if brand:
            brands = set(dealer.brands or [])
            brands.add(str(brand).upper())
            dealer.brands = sorted(brands)
        dealer.seen_at = now_ist_naive()
    session.flush()
    return n


@dataclass
class Step:
    row: int
    vendor_id: int | None
    vendor: str | None
    answer: str
    old_dealer: int | None
    old_where: str | None
    proposed: int | None
    action: str
    reason: str
    proposed_name: str | None = None
    evidence: dict = field(default_factory=dict)


def _header_map(header: list) -> dict[str, int]:
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        t = str(h or "").strip().lower()
        if t == "vendor id":
            cols["vendor_id"] = i
        elif t == "suggested dealer id":
            cols["suggested"] = i
        elif t == "bot verdict":
            cols["verdict"] = i
        elif t.startswith("your answer"):
            cols["answer"] = i
    missing = {"vendor_id", "suggested", "answer"} - set(cols)
    if missing:
        raise ValueError(f"the sheet is missing column(s): {', '.join(sorted(missing))}")
    return cols


def read_sheet(path: str) -> list[tuple[int, dict]]:
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    cols = _header_map(list(rows[0]))
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if r is None or all(v in (None, "") for v in r):
            continue
        out.append((n, {k: (r[i] if i < len(r) else None) for k, i in cols.items()}))
    return out


def _int(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).strip()))
    except ValueError:
        return None


def plan(rows: list[tuple[int, dict]], session: Session, *, accept_exact: bool = False) -> list[Step]:
    from core.models import Vendor

    dealers = {d.dealer_id: d for d in session.execute(select(m.PortalDealer)).scalars()}
    by_name: dict[str, list[int]] = {}
    for d in dealers.values():
        by_name.setdefault(norm_name(d.dealer_name), []).append(d.dealer_id)
    steps: list[Step] = []
    seen: set[int] = set()
    for n, r in rows:
        vendor_id = _int(r.get("vendor_id"))
        raw = str(r.get("answer") or "").strip()
        answer = raw.upper()
        suggested = _int(r.get("suggested"))
        verdict = str(r.get("verdict") or "").strip().upper()
        vendor = session.get(Vendor, vendor_id) if vendor_id else None
        step = Step(n, vendor_id, vendor.name if vendor else None, raw, None, None, None, NOCHANGE, "")
        if vendor is None:
            step.action, step.reason = REJECT, "unknown Bot Vendor ID"
            steps.append(step)
            continue
        if vendor_id in seen:
            step.action, step.reason = REJECT, "vendor appears twice in the sheet -- first row used"
            steps.append(step)
            continue
        seen.add(vendor_id)
        old, where = existing(vendor_id, session)
        step.old_dealer, step.old_where = old, where
        link_row = session.execute(
            select(m.PortalVendorLink).where(m.PortalVendorLink.vendor_id == vendor_id)
        ).scalar_one_or_none()

        # What does the row ask for?
        target = None
        if answer == "Y":
            target = suggested
            if target is None:
                step.action, step.reason = REJECT, "Y but no suggested dealer on this row"
                steps.append(step)
                continue
        elif answer == "N":
            target = "N"
        elif answer == "":
            if accept_exact and old is None and verdict == "LIKELY" and suggested is not None:
                d = dealers.get(suggested)
                same = d is not None and norm_name(d.dealer_name) == norm_name(vendor.name) and norm_name(vendor.name)
                twins = [x for x in by_name.get(norm_name(vendor.name), []) if x != suggested]
                step.evidence = {"portal_name": d.dealer_name if d else None, "exact_name": bool(same),
                                 "other_dealers_same_name": twins, "dealer_brands": (d.brands if d else None)}
                if d is None:
                    step.action, step.reason = REJECT, f"exact-name check: dealer {suggested} not in the Portal dealer table"
                elif not same:
                    step.action, step.reason = REJECT, f"exact-name check failed: Portal name is '{d.dealer_name}'"
                elif twins:
                    step.action, step.reason = REJECT, f"exact-name check: other Portal dealers share the name {twins}"
                else:
                    target = suggested
                    step.reason = "exact name match, validated"
                if target is None:
                    steps.append(step)
                    continue
            else:
                step.action = KEEP if old is not None else NOCHANGE
                step.reason = "blank -- no change" + (" (CHECK/NO MATCH rows are never auto-accepted)"
                                                      if verdict in ("CHECK", "NO MATCH") else "")
                steps.append(step)
                continue
        else:
            target = _int(raw)
            if target is None:
                step.action, step.reason = REJECT, f"answer '{raw}' is not Y, N or a dealer ID"
                steps.append(step)
                continue
            if target not in dealers:
                step.action, step.reason = REJECT, f"dealer {target} is not in the Portal dealer table"
                steps.append(step)
                continue

        # Existing links always win.
        if target == "N":
            if old is not None:
                step.action, step.reason = KEEP, f"N ignored -- existing link to {old} is kept"
            elif link_row is not None and link_row.status == m.LINK_ONBOARDING:
                step.action, step.reason = NOCHANGE, "already marked onboarding required"
            else:
                step.action, step.reason = ONBOARD, "not on the Portal -- onboarding required"
            steps.append(step)
            continue
        step.proposed = target
        step.proposed_name = dealers[target].dealer_name if target in dealers else None
        if old == target:
            step.action, step.reason = ALREADY, "already linked to this dealer -- no change"
        elif old is not None:
            # An existing link always wins -- checked before anything else.
            step.action = KEEP if answer == "Y" or answer == "" else CONFLICT
            step.reason = (f"existing link to {old} kept; suggestion {target} ignored" if step.action == KEEP
                           else f"existing link to {old} NOT changed to {target} -- fix it by hand if really wrong")
        elif target not in dealers:
            step.action, step.reason = REJECT, f"dealer {target} is not in the Portal dealer table"
        else:
            step.action = LINK
            step.reason = step.reason or ("Y -- suggested dealer accepted" if answer == "Y" else "dealer ID typed")
        steps.append(step)
    return steps


def apply(steps: list[Step], session: Session, *, source: str, decided_by: str) -> dict:
    """Commit exactly the LINK / ONBOARD steps of a plan. Nothing is deleted."""
    done = {LINK: 0, ONBOARD: 0}
    vendors: list[int] = []
    for s in steps:
        if s.action not in (LINK, ONBOARD) or s.vendor_id is None:
            continue
        old, _ = existing(s.vendor_id, session)
        if s.action == LINK and old is not None:
            continue  # someone linked it meanwhile -- existing wins
        row = session.execute(
            select(m.PortalVendorLink).where(m.PortalVendorLink.vendor_id == s.vendor_id)
        ).scalar_one_or_none()
        if row is None:
            row = m.PortalVendorLink(vendor_id=s.vendor_id)
            session.add(row)
        if s.action == LINK:
            row.dealer_id, row.dealer_name, row.status = s.proposed, s.proposed_name, m.LINK_LINKED
        else:
            if row.status == m.LINK_LINKED and row.dealer_id:
                continue  # N never deletes a link
            row.dealer_id, row.status = None, m.LINK_ONBOARDING
        row.source, row.decided_by, row.updated_at = source, decided_by, now_ist_naive()
        done[s.action] += 1
        vendors.append(s.vendor_id)
    session.flush()
    # Quotes waiting for onboarding are retried at once for the newly linked vendors.
    if vendors:
        for alloc in session.execute(
            select(m.PortalAllocation).where(
                m.PortalAllocation.vendor_id.in_(vendors),
                m.PortalAllocation.state == m.VENDOR_ONBOARDING_REQUIRED,
            )
        ).scalars():
            alloc.next_try_at = None
        session.flush()
    logger.info("Portal vendor links: %d linked, %d marked onboarding (%s).", done[LINK], done[ONBOARD], source)
    return done
