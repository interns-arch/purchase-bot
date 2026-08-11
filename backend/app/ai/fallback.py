"""AI-ASSISTED RESCUE for Vendor Inventory files the deterministic parser
could not read (Phase 3 of ARCHITECTURE_V2 -- gated by AI_FALLBACK_ENABLED).

Design principle: **the model never writes data -- it only proposes WHICH
columns to use.** The pipeline is:

    deterministic parse FAILED (unknown headers)
        -> model reads a token-bounded sample of the grid
        -> strict schema + validation gate (`normalized_validation`:
           money/MRP/discount/float-stock can NEVER become quantity,
           confidence floor, quantities re-parsed server-side)
        -> the proposed column mapping is applied DETERMINISTICALLY to the
           file (`inventory_import_service.read_table_with_mapping`) and the
           model's own sampled rows are cross-checked against that re-read:
           if the model "read" even one (part, quantity) pair that the file
           does not actually contain, the rescue is refused
        -> only then does the caller re-run the UNCHANGED `run_import` with
           the mapping, so every imported value comes from the file and every
           existing guard (duplicates, row limit, blank/negative rows,
           zero-row failure, supersession) still applies.

Never raises into business code: any failure returns None and the original
deterministic FAILED result stands.
"""

from __future__ import annotations

from pathlib import Path

from backend.app.ai.compact import compact_grid
from backend.app.ai.provider import UnderstandRequest
from backend.app.ai.registry import document_fallback_enabled, get_provider
from backend.app.ai.schemas import VENDOR_INVENTORY
from backend.app.core.config import settings
from core.ingestion.column_detector import normalise_part_number, parse_quantity, is_parseable_quantity
from core.ingestion.csv_reader import read_csv_grid
from core.ingestion.excel_reader import read_excel_grid
from core.logging_setup import get_logger
from core.services import inventory_import_service
from core.services.normalized_validation import validate_normalized_document

logger = get_logger(__name__)

# Deterministic failures the model can plausibly rescue: unknown/undetected
# headers or a header the deterministic scanner picked that yielded no rows.
_RESCUE_ELIGIBLE_FRAGMENTS = (
    "column not found",
    "no inventory header row",
    "no inventory rows could be imported",
)


def rescue_eligible(message: str | None) -> bool:
    text = (message or "").lower()
    return any(fragment in text for fragment in _RESCUE_ELIGIBLE_FRAGMENTS)


def provenance_label() -> str:
    """Short "who taught us this" label stored with a learned format."""
    try:
        provider = get_provider()
        model = getattr(provider, "_model", None) or ""
        return f"ai:{provider.name}{'/' + model if model else ''}"
    except Exception:  # noqa: BLE001
        return "ai"


def _read_grid(file_path: Path) -> list[list[str]] | None:
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".csv":
            return read_csv_grid(file_path)
        if suffix in (".xlsx", ".xlsm", ".xls"):
            return read_excel_grid(file_path)
    except Exception:  # noqa: BLE001 -- rescue must never raise
        logger.exception("AI rescue: could not read %s as a grid.", file_path.name)
        return None
    return None


def discover_inventory_mapping(
    file_path: Path, deterministic_reason: str | None
) -> tuple[dict[str, str], str] | None:
    """Ask the configured model to read the failed file and return a VERIFIED
    (column_mapping, provenance_note) -- or None, leaving the deterministic
    failure in place. See module docstring for the guard chain."""
    if not document_fallback_enabled():
        return None

    try:
        grid = _read_grid(file_path)
        if not grid:
            return None

        provider = get_provider()
        compact_text = compact_grid(
            grid, file_name=file_path.name, max_rows=settings.ai_max_rows_sample
        )
        logger.info(
            "AI rescue: deterministic parser failed for %s (%s) -- asking %s to "
            "propose a column mapping.",
            file_path.name,
            (deterministic_reason or "no reason recorded")[:120],
            provider.name,
        )
        document = provider.understand_document(
            UnderstandRequest(
                document_type=VENDOR_INVENTORY,
                compact_text=compact_text,
                file_name=file_path.name,
                hints={"deterministic_failure": (deterministic_reason or "")[:200]},
            )
        )
        if document is None:
            logger.info("AI rescue: provider returned no usable result for %s.", file_path.name)
            return None

        # Gate 1: strict validation (anti-mis-mapping, confidence floor,
        # server-side quantity re-parse).
        result = validate_normalized_document(
            document, minimum_confidence=settings.ai_confidence_threshold
        )
        if not result.is_valid or not result.rows:
            logger.info(
                "AI rescue REFUSED for %s: validator says %s.",
                file_path.name,
                result.reason[:160] or "no valid rows",
            )
            return None

        mapping = dict(document.meta.column_mapping or {})

        # Gate 2: apply the mapping deterministically and cross-check every
        # sampled model row against what the file ACTUALLY contains.
        try:
            _headers, file_rows, part_col, qty_col = (
                inventory_import_service.read_table_with_mapping(file_path, mapping)
            )
        except ValueError as exc:
            logger.info("AI rescue REFUSED for %s: mapping unusable (%s).", file_path.name, exc)
            return None

        actual_pairs = set()
        for row in file_rows:
            part = (row.get(part_col) or "").strip()
            raw_qty = row.get(qty_col, "")
            if part and is_parseable_quantity(raw_qty):
                actual_pairs.add((normalise_part_number(part), parse_quantity(raw_qty)))

        for model_row in result.rows:
            pair = (normalise_part_number(model_row.part_number), model_row.quantity)
            if pair not in actual_pairs:
                logger.warning(
                    "AI rescue REFUSED for %s: model row %s is not present in the "
                    "file under the proposed mapping -- possible misread.",
                    file_path.name,
                    pair,
                )
                return None

        note = (
            f"Imported via AI-assisted column mapping ({provider.name}"
            f"{'/' + document.meta.model if document.meta.model else ''}): "
            f"part_number={mapping.get('part_number')!r}, "
            f"quantity={mapping.get('available_quantity') or mapping.get('quantity')!r} "
            f"(confidence {document.meta.confidence:.2f}"
            + (
                f"; ignored money columns: {', '.join(document.meta.rejected_columns)}"
                if document.meta.rejected_columns
                else ""
            )
            + "). All values read from the file itself."
        )
        logger.info(
            "AI rescue ACCEPTED for %s: %s (cross-checked %d model row(s) against "
            "%d file row(s)).",
            file_path.name,
            note,
            len(result.rows),
            len(file_rows),
        )
        return mapping, note

    except Exception:  # noqa: BLE001 -- rescue must never affect the import outcome
        logger.exception("AI rescue failed for %s (deterministic result stands).", file_path.name)
        return None
