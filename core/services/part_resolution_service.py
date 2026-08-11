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

# IN-clause / executemany chunk size: comfortably below SQLite's historical
# 999-variable limit and keeps Postgres statements reasonably sized.
_CHUNK = 500


def _insert_ignore_conflicts(session: Session, table, rows: list[dict]) -> None:
    """Bulk INSERT ... ON CONFLICT DO NOTHING (Postgres + SQLite). A row that
    a concurrent import created first is simply skipped -- the caller
    re-reads afterwards, so the winner's row is always the one used."""
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    for start in range(0, len(rows), _CHUNK):
        statement = dialect_insert(table).values(rows[start : start + _CHUNK])
        session.execute(statement.on_conflict_do_nothing())


def resolve_parts_bulk(
    vendor_id: int, raw_part_numbers: list[str], session: Session
) -> dict[str, Part]:
    """Resolve MANY raw part numbers in a fixed handful of statements instead
    of 2-3 round-trips per row -- same results and same guarantees as calling
    `resolve_part` per row (canonical part reused across vendors, one alias
    per vendor+normalized, concurrent imports race-safe via ON CONFLICT), but
    a 6,800-line file costs ~30 statements instead of ~20,000.

    Returns {normalized_part_number: Part} covering every non-blank input."""
    first_raw_by_norm: dict[str, str] = {}
    for raw in raw_part_numbers:
        raw = (raw or "").strip()
        if not raw:
            continue
        norm = normalise_part_number(raw)
        if norm and norm not in first_raw_by_norm:
            first_raw_by_norm[norm] = raw
    wanted = list(first_raw_by_norm)
    if not wanted:
        return {}

    def _chunked_select(model, column, values):
        found = []
        for start in range(0, len(values), _CHUNK):
            found += list(
                session.execute(
                    select(model).where(column.in_(values[start : start + _CHUNK]))
                ).scalars()
            )
        return found

    # 1. Aliases this vendor already has -> already fully resolved.
    resolved: dict[str, Part] = {}
    for start in range(0, len(wanted), _CHUNK):
        for alias in session.execute(
            select(PartAlias).where(
                PartAlias.vendor_id == vendor_id,
                PartAlias.normalized_part_number.in_(wanted[start : start + _CHUNK]),
            )
        ).scalars():
            resolved[alias.normalized_part_number] = alias.part
    missing = [n for n in wanted if n not in resolved]

    if missing:
        # 2. Canonical parts that already exist (other vendors' imports).
        parts_by_norm = {
            part.canonical_part_number: part
            for part in _chunked_select(Part, Part.canonical_part_number, missing)
        }
        # 3. Create every missing canonical part in bulk; conflicts (a
        #    concurrent import inserted first) are skipped, then ONE re-read
        #    picks up whichever row won.
        to_create = [n for n in missing if n not in parts_by_norm]
        _insert_ignore_conflicts(
            session, Part.__table__, [{"canonical_part_number": n} for n in to_create]
        )
        if to_create:
            parts_by_norm.update(
                {
                    part.canonical_part_number: part
                    for part in _chunked_select(Part, Part.canonical_part_number, to_create)
                }
            )
        # 4. Create this vendor's missing aliases in bulk (same conflict rule).
        _insert_ignore_conflicts(
            session,
            PartAlias.__table__,
            [
                {
                    "part_id": parts_by_norm[n].id,
                    "vendor_id": vendor_id,
                    "vendor_part_number": first_raw_by_norm[n],
                    "normalized_part_number": n,
                }
                for n in missing
                if n in parts_by_norm
            ],
        )
        resolved.update({n: parts_by_norm[n] for n in missing if n in parts_by_norm})

    return resolved


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
