"""Learned file-format memory (backed by `LearnedFileFormat`).

The LLM analyses any given vendor-file format AT MOST ONCE: when an
AI-assisted rescue succeeds, its verified column mapping is saved here keyed
by the header row's fingerprint. The next file with the same header layout
resolves deterministically from this table -- zero model calls, zero
latency, zero API cost. Formats can also be seeded manually (learned_from
= "manual") to pre-define a known layout without ever involving the model.

Pure business logic -- no AI/provider imports here; mappings are plain
column-name dicts consumed by `inventory_import_service.read_table_with_mapping`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.ingestion.column_detector import normalise_header
from core.ingestion.csv_reader import read_csv_grid
from core.ingestion.excel_reader import read_excel_grid
from core.logging_setup import get_logger
from core.models import LearnedFileFormat

logger = get_logger(__name__)


def _fingerprint_of_row(row) -> str | None:
    cells = [normalise_header(str(cell)) for cell in row if str(cell).strip()]
    cells = [cell for cell in cells if cell]
    if len(cells) < 2:  # a header row has at least a part and a quantity column
        return None
    return "|".join(cells)


def _read_grid(file_path: Path):
    if file_path.suffix.lower() == ".csv":
        return read_csv_grid(file_path)
    return read_excel_grid(file_path)


def find_mapping(file_path: Path, session: Session) -> tuple[dict[str, str], str] | None:
    """Scan the file's rows for a header layout the system already knows.
    Returns ({part_number, available_quantity}, provenance_note) or None.
    Never raises -- an unreadable file simply returns None."""
    try:
        grid = _read_grid(file_path)
    except Exception:  # noqa: BLE001 -- lookup must never break the import path
        logger.exception("Format memory: could not read %s as a grid.", file_path.name)
        return None

    fingerprints = {}
    for row in grid[:100]:
        fp = _fingerprint_of_row(row)
        if fp and fp not in fingerprints:
            fingerprints[fp] = True
    if not fingerprints:
        return None

    row_obj = session.execute(
        select(LearnedFileFormat).where(
            LearnedFileFormat.header_fingerprint.in_(list(fingerprints))
        )
    ).scalars().first()
    if row_obj is None:
        return None

    row_obj.use_count = (row_obj.use_count or 0) + 1
    row_obj.last_used_at = datetime.utcnow()
    session.flush()

    mapping = {
        "part_number": row_obj.part_column,
        "available_quantity": row_obj.quantity_column,
    }
    note = (
        f"Imported via saved format #{row_obj.id} "
        f"(learned {row_obj.learned_from}; used {row_obj.use_count}x): "
        f"part_number={row_obj.part_column!r}, quantity={row_obj.quantity_column!r}. "
        "No AI call was needed."
    )
    logger.info(
        "Format memory HIT for %s: format #%s (%s), use_count=%s.",
        file_path.name,
        row_obj.id,
        row_obj.learned_from,
        row_obj.use_count,
    )
    return mapping, note


def save_mapping(
    headers: list[str],
    mapping: dict[str, str],
    learned_from: str,
    session: Session,
) -> None:
    """Remember a VERIFIED mapping for this header layout (idempotent: an
    existing fingerprint is updated, not duplicated). `headers` must be the
    actual header row the mapping was applied to."""
    fp = _fingerprint_of_row(headers)
    if fp is None:
        return
    part_column = (mapping.get("part_number") or "").strip()
    quantity_column = (
        mapping.get("available_quantity") or mapping.get("quantity") or ""
    ).strip()
    if not part_column or not quantity_column:
        return

    existing = session.execute(
        select(LearnedFileFormat).where(LearnedFileFormat.header_fingerprint == fp)
    ).scalar_one_or_none()
    if existing is not None:
        existing.part_column = part_column
        existing.quantity_column = quantity_column
        existing.learned_from = learned_from
    else:
        session.add(
            LearnedFileFormat(
                header_fingerprint=fp,
                part_column=part_column,
                quantity_column=quantity_column,
                learned_from=learned_from,
                sample_headers={"headers": headers},
            )
        )
    session.flush()
    logger.info(
        "Format memory SAVED: %r -> part=%r qty=%r (from %s).",
        fp[:80],
        part_column,
        quantity_column,
        learned_from,
    )
