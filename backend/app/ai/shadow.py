"""SHADOW MODE: observe what the model *would* have extracted, change nothing.

Runs only when the deterministic Vendor Inventory parser has already FAILED.
It builds the compact representation, asks the configured provider to read it,
runs the result through the Phase 1 strict validator, and LOGS the outcome.

The result is never imported. Two structural guarantees, not just intentions:

1. **No database access is possible here.** No function in this module accepts
   a `Session`, and the module imports no model/ORM/service that writes. It
   physically cannot create or update `VendorInventory`, `Part`, `PartAlias`,
   `Vendor`, `VendorSelection` or anything else.
2. **No business decision is made here.** Nothing is returned to the caller
   that can influence the import outcome -- `observe_failed_inventory()` returns
   a report object purely for logging, and the caller ignores it. The document
   keeps the deterministic FAILED/NEEDS_REVIEW status it already had.

Never raises: any failure (provider down, timeout, bad JSON, validator
rejection, unreadable file) is caught and recorded in the returned report, so
shadow mode can never affect an import.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from backend.app.ai.compact import compact_grid
from backend.app.ai.provider import UnderstandRequest
from backend.app.ai.registry import get_provider, shadow_mode_enabled
from backend.app.ai.schemas import VENDOR_INVENTORY
from backend.app.core.config import settings
from core.ingestion.csv_reader import read_csv_grid
from core.ingestion.excel_reader import read_excel_grid
from core.logging_setup import get_logger
from core.services.normalized_validation import validate_normalized_document

logger = get_logger(__name__)

SKIPPED_DISABLED = "shadow mode disabled"
SKIPPED_UNSUPPORTED = "unsupported file type for grid extraction"


@dataclass
class ShadowOutcome:
    """A report, not a decision. The caller logs it and moves on."""

    attempted: bool = False
    provider: str | None = None
    model: str | None = None
    skipped_reason: str | None = None
    error: str | None = None

    extracted_rows: int = 0
    confidence: float = 0.0
    column_mapping: dict[str, str] | None = None
    rejected_columns: list[str] | None = None
    vendor_name: str | None = None

    validator_accepted: bool = False
    validator_reason: str = ""
    validated_rows: int = 0
    sample: list[tuple[str, str]] | None = None

    duration_ms: int = 0

    @property
    def would_have_imported(self) -> bool:
        """Purely informational: would the LLM path have produced usable rows
        if it had been enabled? Never acted on in shadow mode."""
        return self.attempted and self.validator_accepted and self.validated_rows > 0


def _read_grid(file_path: Path) -> list[list[str]] | None:
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".csv":
            return read_csv_grid(file_path)
        if suffix in (".xlsx", ".xlsm", ".xls"):
            return read_excel_grid(file_path)
    except Exception:  # noqa: BLE001 -- observation must never raise
        logger.exception("Shadow: could not read %s as a grid.", file_path.name)
        return None
    return None


def observe_failed_inventory(
    file_path: Path, *, deterministic_reason: str | None = None
) -> ShadowOutcome:
    """Observe-only analysis of a Vendor Inventory file the deterministic
    parser could not read. Returns a report; writes nothing anywhere."""
    outcome = ShadowOutcome()

    if not shadow_mode_enabled():
        outcome.skipped_reason = SKIPPED_DISABLED
        return outcome

    started = time.monotonic()
    try:
        grid = _read_grid(file_path)
        if not grid:
            outcome.skipped_reason = SKIPPED_UNSUPPORTED
            return outcome

        provider = get_provider()
        outcome.provider = provider.name
        outcome.attempted = True

        compact_text = compact_grid(
            grid, file_name=file_path.name, max_rows=settings.ai_max_rows_sample
        )
        logger.info(
            "Shadow: deterministic parser failed for %s (%s) -- asking %s to read "
            "%d-row grid (compact sample %d chars).",
            file_path.name,
            (deterministic_reason or "no reason recorded")[:120],
            provider.name,
            len(grid),
            len(compact_text),
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
            outcome.error = "provider returned no usable result"
            logger.info("Shadow: %s -> provider returned None.", file_path.name)
            return outcome

        outcome.model = document.meta.model
        outcome.confidence = document.meta.confidence
        outcome.column_mapping = dict(document.meta.column_mapping or {})
        outcome.rejected_columns = list(document.meta.rejected_columns or [])
        outcome.vendor_name = document.vendor_name
        outcome.extracted_rows = len(document.rows)

        result = validate_normalized_document(
            document, minimum_confidence=settings.ai_confidence_threshold
        )
        outcome.validator_accepted = result.is_valid
        outcome.validator_reason = result.reason
        outcome.validated_rows = len(result.rows)
        outcome.sample = [(row.part_number, str(row.quantity)) for row in result.rows[:5]]

        logger.info(
            "Shadow RESULT %s | provider=%s model=%s | extracted=%d rows conf=%.2f | "
            "mapping=%s rejected=%s | validator=%s (%s) | validated_rows=%d | sample=%s | "
            "NOTHING WRITTEN TO DATABASE",
            file_path.name,
            outcome.provider,
            outcome.model,
            outcome.extracted_rows,
            outcome.confidence,
            outcome.column_mapping,
            outcome.rejected_columns,
            "ACCEPTED" if outcome.validator_accepted else "REJECTED",
            outcome.validator_reason[:160] or "-",
            outcome.validated_rows,
            outcome.sample,
        )
        return outcome

    except Exception as exc:  # noqa: BLE001 -- shadow mode must never affect an import
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger.exception("Shadow analysis failed for %s (import is unaffected).", file_path.name)
        return outcome
    finally:
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
