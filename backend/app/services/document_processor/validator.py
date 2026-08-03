"""Cheap fail-fast checks that run before classification -- NOT a
re-implementation of the deeper validation each `core.services.*` import
function already does (column detection, row-level checks); this only
rejects obviously-wrong files before any of that runs."""

from __future__ import annotations

from pathlib import Path

STRUCTURED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}
INVOICE_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = STRUCTURED_EXTENSIONS | INVOICE_EXTENSIONS


class DocumentValidationError(Exception):
    """Raised when a file fails the pre-classification sanity check."""


def validate_file(file_path: Path) -> None:
    if not file_path.exists():
        raise DocumentValidationError(f"'{file_path.name}' could not be found after upload.")

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise DocumentValidationError(
            f"Unsupported file type '{file_path.suffix}'. "
            f"Supported: {sorted(SUPPORTED_EXTENSIONS)}."
        )

    if file_path.stat().st_size == 0:
        raise DocumentValidationError(f"'{file_path.name}' is empty.")
