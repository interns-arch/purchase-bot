"""Vendor lookup/creation. There is no manual vendor management in this
application -- vendors are only ever auto-created from an imported
inventory file's name (see `document_processor.dispatcher`). Pure business
logic -- no print()/input() here."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.logging_setup import get_logger
from core.models import Vendor

logger = get_logger(__name__)


def create_vendor(
    name: str,
    session: Session,
    *,
    contact_info: str | None = None,
    payment_terms: str | None = None,
    whatsapp_number: str | None = None,
) -> Vendor:
    name = name.strip()
    if not name:
        raise ValueError("Vendor name cannot be blank.")

    if get_vendor_by_name(name, session) is not None:
        raise ValueError(f"A vendor named '{name}' already exists.")

    vendor = Vendor(
        name=name,
        contact_info=contact_info,
        payment_terms=payment_terms,
        whatsapp_number=whatsapp_number,
    )
    session.add(vendor)
    session.flush()  # assign vendor.id
    return vendor


def get_vendor(vendor_id: int, session: Session) -> Vendor | None:
    return session.get(Vendor, vendor_id)


def list_vendors(session: Session) -> list[Vendor]:
    """Every vendor in the database, ordered by vendor code then name --
    used by read-only outputs (e.g. the consolidated Vendor Inventory
    workbook) that need to enumerate all vendors, not just one."""
    return list(
        session.execute(
            select(Vendor).order_by(func.coalesce(Vendor.vendor_code, Vendor.name), Vendor.name)
        ).scalars()
    )


import re as _re

# Filler words that do NOT distinguish one vendor from another (the Founder's
# rule: "Bijwasan" and "Bijwasan Stock" are the SAME entity). Only generic
# stock-file words are listed -- real name words are never stripped, so
# distinct vendors (aman vs amit) can never merge.
_VENDOR_NAME_FILLERS = {"stock", "stocks", "inventory", "stocklist"}


def normalise_vendor_name(name: str) -> str:
    """Identity form of a vendor name: lowercase, alphanumeric words only,
    with generic filler words ('stock' etc.) removed. 'BIJWASHAN STOCK' ->
    'bijwashan'; 'Delhi Branch Stock' -> 'delhi branch'."""
    words = _re.split(r"[^a-z0-9]+", (name or "").strip().lower())
    kept = [word for word in words if word and word not in _VENDOR_NAME_FILLERS]
    return " ".join(kept) or (name or "").strip().lower()


import os as _os

# NEAR-MISS SPELLINGS (Founder: "jaipur, Japur, JAIPUR ... all must get the
# same code"). Case, spacing, punctuation and filler words are handled above
# and need no tolerance at all. This adds ONE more step: a name that differs
# from an existing vendor by a single letter -- a dropped 'h' (BIJWASHAN /
# BIJWASAN), a swapped letter (BIJVASAN / BIJWASAN), a missing vowel (JAIPUR
# / JAPUR), or two letters typed in the wrong order.
#
# The guardrails are what keep this safe; every one of them exists because
# of a real pair in the live vendor list:
#   * both names must be at least _FUZZY_MIN_LENGTH characters -- 'aman' and
#     'amit' are 4, so they are never even considered;
#   * exactly ONE existing vendor may be within one letter. Two candidates
#     means we cannot know which, so nothing is guessed;
#   * a difference in a DIGIT never matches -- 'v01 apex' / 'v02 apex' and
#     'stock10' / 'stock11' are different vendors, not typos.
# Set VENDOR_NAME_FUZZY_ENABLED=false to turn the whole step off.
_FUZZY_MIN_LENGTH = 5


def _fuzzy_enabled() -> bool:
    return _os.environ.get("VENDOR_NAME_FUZZY_ENABLED", "true").strip().lower() != "false"


def _within_one_edit(left: str, right: str) -> bool:
    """True when `left` becomes `right` with ONE substitution, insertion,
    deletion, or swap of two neighbouring characters. Digits never differ:
    a changed number means a different vendor, not a typo."""
    if left == right:
        return True

    length_left, length_right = len(left), len(right)
    if abs(length_left - length_right) > 1:
        return False

    if length_left == length_right:
        differing = [i for i in range(length_left) if left[i] != right[i]]
        if len(differing) == 1:
            index = differing[0]
            return not (left[index].isdigit() or right[index].isdigit())
        if len(differing) == 2:  # neighbouring swap, e.g. 'jaipur'/'jaiupr'
            first, second = differing
            if second != first + 1:
                return False
            if any(ch.isdigit() for ch in (left[first], left[second])):
                return False
            return left[first] == right[second] and left[second] == right[first]
        return False

    # One is exactly one character longer: it must be an insertion.
    longer, shorter = (left, right) if length_left > length_right else (right, left)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1 :] == shorter:
            return not longer[index].isdigit()
    return False


def find_similar_vendor(name: str, session: Session) -> Vendor | None:
    """The ONE existing vendor whose name is a single-letter variant of
    `name`, or None when there is no candidate, more than one, or the names
    are too short to judge. Never raises."""
    if not _fuzzy_enabled():
        return None
    wanted = normalise_vendor_name(name)
    if len(wanted) < _FUZZY_MIN_LENGTH:
        return None

    matches: list[Vendor] = []
    for vendor in session.execute(select(Vendor)).scalars():
        candidate = normalise_vendor_name(vendor.name)
        if len(candidate) < _FUZZY_MIN_LENGTH or candidate == wanted:
            continue
        if _within_one_edit(wanted, candidate):
            matches.append(vendor)
            if len(matches) > 1:
                # Ambiguous -- refuse to guess between two real vendors.
                return None
    return matches[0] if matches else None


def get_vendor_by_name(name: str, session: Session) -> Vendor | None:
    """Match by name, tolerating case and generic filler words: a file
    captioned 'BIJWASHAN STOCK' reuses the existing 'Bijwasan Stock' /
    'BIJWASHAN' vendor instead of onboarding a duplicate. Exact
    (case-insensitive) match wins first; the filler-insensitive match only
    runs when that finds nothing."""
    exact = session.execute(
        select(Vendor).where(func.lower(Vendor.name) == name.strip().lower())
    ).scalar_one_or_none()
    if exact is not None:
        return exact

    wanted = normalise_vendor_name(name)
    if not wanted:
        return None
    for vendor in session.execute(select(Vendor)).scalars():
        if normalise_vendor_name(vendor.name) == wanted:
            return vendor

    # REMEMBERED aliases (Founder's rule): a name that once belonged to a
    # merged-away duplicate (e.g. 'BIJWASHAN STOCK' after merging into
    # 'Bijvasan') resolves to the vendor it was merged into -- forever.
    from core.models import VendorNameAlias

    alias = session.execute(
        select(VendorNameAlias).where(VendorNameAlias.normalized_name == wanted)
    ).scalar_one_or_none()
    if alias is not None:
        return session.get(Vendor, alias.vendor_id)

    # Last: a single-letter spelling variant ('Japur' -> 'jaipur'). Runs only
    # after every exact route has failed, so it can never override a real
    # match -- see `find_similar_vendor` for the guardrails.
    similar = find_similar_vendor(name, session)
    if similar is not None:
        logger.info(
            "Vendor name %r matched existing vendor '%s' (id=%s, code=%s) as a "
            "one-letter spelling variant -- reusing it instead of onboarding a "
            "duplicate.",
            name,
            similar.name,
            similar.id,
            similar.vendor_code,
        )
    return similar


class VendorNameInUseError(ValueError):
    """The spelling being declared already belongs to a DIFFERENT vendor that
    holds its own stock. An alias would be ignored (a real vendor always wins
    the match), so the two vendors must be MERGED instead."""

    def __init__(self, existing: Vendor):
        self.existing = existing
        super().__init__(
            f"'{existing.name}' (#{existing.id}) already exists as its own vendor."
        )


def remember_vendor_name(
    alias_name: str, vendor_id: int, session: Session, *, declared_by: str | None = None
) -> dict:
    """Declare that a SPELLING belongs to an existing vendor, permanently.

    Case and filler words are already handled automatically ('JAIPUR STOCK'
    finds 'jaipur' on its own). This is for spellings no rule can safely
    guess -- 'BIJVASAN' vs 'BIJWASHAN', 'northern distributer' vs 'Northend
    Distributors' -- where only a human knows they are one firm. Loose
    automatic matching is deliberately NOT used: it would also merge genuinely
    different vendors such as 'aman' and 'amit'.

    From then on, any file captioned with that spelling imports under the
    vendor kept here instead of onboarding a new duplicate."""
    from core.models import VendorNameAlias

    normalized = normalise_vendor_name(alias_name)
    if not normalized:
        raise ValueError("The alias name must contain letters or digits.")

    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise ValueError(f"No vendor with id {vendor_id}.")

    if normalise_vendor_name(vendor.name) == normalized:
        return {"action": "already_matches", "vendor": vendor.name, "alias": normalized}

    # A live vendor with this identity would always win the lookup before the
    # alias is consulted, so recording one would be a silent no-op.
    for other in session.execute(select(Vendor)).scalars():
        if other.id != vendor.id and normalise_vendor_name(other.name) == normalized:
            raise VendorNameInUseError(other)

    existing = session.execute(
        select(VendorNameAlias).where(VendorNameAlias.normalized_name == normalized)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.vendor_id == vendor.id:
            return {"action": "already_declared", "vendor": vendor.name, "alias": normalized}
        previous_id = existing.vendor_id
        existing.vendor_id = vendor.id
        session.flush()
        return {
            "action": "repointed",
            "vendor": vendor.name,
            "alias": normalized,
            "previous_vendor_id": previous_id,
        }

    session.add(VendorNameAlias(normalized_name=normalized, vendor_id=vendor.id))
    session.flush()
    return {"action": "declared", "vendor": vendor.name, "alias": normalized}


def list_vendor_name_aliases(session: Session) -> list[tuple[str, str]]:
    """[(declared spelling, vendor name)] for every remembered alias."""
    from core.models import VendorNameAlias

    rows = session.execute(
        select(VendorNameAlias.normalized_name, Vendor.name)
        .join(Vendor, Vendor.id == VendorNameAlias.vendor_id)
        .order_by(Vendor.name, VendorNameAlias.normalized_name)
    ).all()
    return [(alias, vendor_name) for alias, vendor_name in rows]


def get_vendor_by_whatsapp_number(number: str, session: Session) -> Vendor | None:
    number = number.strip()
    if not number:
        return None
    return session.execute(
        select(Vendor).where(Vendor.whatsapp_number == number)
    ).scalar_one_or_none()


