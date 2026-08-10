"""Canonical Part Master automation.

Every new normalized part number seen during an import automatically gets a
canonical `parts` row (if none matches that key yet) and always gets a
`part_aliases` row linking the vendor's raw code to it. No manual curation
step is required in this phase.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.ingestion.column_detector import normalise_part_number
from core.models import Part, PartAlias


def resolve_part(vendor_id: int, raw_part_number: str, session: Session) -> Part:
    """Find or create the canonical Part for a vendor's raw part number.

    Resolution order:
    1. An alias already exists for this vendor + normalized code -> reuse its part.
    2. A canonical part already exists for this normalized code (created by a
       different vendor's import) -> reuse it, add a new alias.
    3. Neither exists -> create both the canonical part and the alias.
    """
    normalized = normalise_part_number(raw_part_number)

    existing_alias = session.execute(
        select(PartAlias).where(
            PartAlias.vendor_id == vendor_id,
            PartAlias.normalized_part_number == normalized,
        )
    ).scalar_one_or_none()

    if existing_alias is not None:
        return existing_alias.part

    part = session.execute(
        select(Part).where(Part.canonical_part_number == normalized)
    ).scalar_one_or_none()

    if part is None:
        # Different vendors' files routinely contain the SAME part numbers, and
        # imports run concurrently (one thread per WhatsApp document). Both
        # sessions can pass the SELECT above before either commits, so this
        # INSERT can violate the parts.canonical_part_number unique constraint.
        # Insert under a SAVEPOINT so the violation poisons only the savepoint
        # (not the whole import transaction), then re-read the winner's row.
        try:
            with session.begin_nested():
                part = Part(canonical_part_number=normalized)
                session.add(part)
        except IntegrityError:
            part = session.execute(
                select(Part).where(Part.canonical_part_number == normalized)
            ).scalar_one_or_none()
            if part is None:
                raise

    # Same race for the alias (e.g. the same vendor file delivered twice at
    # once): recover by reusing the alias the concurrent import created.
    try:
        with session.begin_nested():
            alias = PartAlias(
                part_id=part.id,
                vendor_id=vendor_id,
                vendor_part_number=raw_part_number,
                normalized_part_number=normalized,
            )
            session.add(alias)
    except IntegrityError:
        existing = session.execute(
            select(PartAlias).where(
                PartAlias.vendor_id == vendor_id,
                PartAlias.normalized_part_number == normalized,
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing.part

    return part
