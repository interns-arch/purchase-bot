"""Upload orchestration for Inventory Import. Framework-specific glue only --
saving the uploaded bytes to disk and picking which vendor a file belongs to.
All actual import logic (delimiter/column detection, validation, the
active/superseded/duplicate state machine) lives in
`core.services.inventory_import_service.run_import`, called here unchanged.

Vendor resolution mirrors the existing `inventory_import.py` CLI script: if
the caller doesn't pin every file to one vendor, each file's vendor is
derived from its filename (auto-creating the vendor on first sight) -- the
same behavior as dropping files into `raw_files/`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from core.logging_setup import get_logger
from core.models import ImportStatus
from core.services import inventory_import_service as import_service
from core.services import vendor_service

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
UPLOAD_ROOT = REPO_ROOT / settings.upload_dir
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class UploadOutcome:
    file_name: str
    vendor_id: int | None
    vendor_name: str | None
    import_id: int | None
    status: str
    is_duplicate: bool
    row_count: int
    error_count: int
    message: str | None
    error: str | None = None


def _vendor_name_from_filename(filename: str) -> str:
    return Path(filename).stem.strip()


def _safe_component(text: str) -> str:
    return _UNSAFE_CHARS.sub("_", text).strip("_") or "file"


def _save_upload(upload: UploadFile, vendor_name: str) -> Path:
    # The uniquifying timestamp/uuid goes on the DIRECTORY, not the filename
    # itself -- `run_import` records `file_path.name` as the import's
    # `file_name` (used for both history display and, in other services,
    # duplicate detection), so it must stay the real original name.
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8]
    dest_dir = UPLOAD_ROOT / _safe_component(vendor_name) / f"{timestamp}_{unique_suffix}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / _safe_component(upload.filename or "upload")

    with dest_path.open("wb") as out_file:
        while chunk := upload.file.read(1024 * 1024):
            out_file.write(chunk)

    return dest_path


def _get_or_create_vendor(name: str, session: Session):
    vendor = vendor_service.get_vendor_by_name(name, session)
    if vendor is None:
        vendor = vendor_service.create_vendor(name, session)
        logger.info("New vendor created from upload: '%s' (id=%s)", vendor.name, vendor.id)
    return vendor


def process_uploads(
    files: list[UploadFile], session: Session, *, vendor_id: int | None = None
) -> list[UploadOutcome]:
    """Import every uploaded file. If `vendor_id` is given, every file is
    imported against that vendor; otherwise each file's vendor is derived
    from its own filename."""
    forced_vendor = None
    if vendor_id is not None:
        forced_vendor = vendor_service.get_vendor(vendor_id, session)
        if forced_vendor is None:
            raise ValueError(f"Vendor {vendor_id} does not exist.")

    outcomes: list[UploadOutcome] = []

    for upload in files:
        file_name = upload.filename or "upload"
        try:
            # Each file gets its own SAVEPOINT: `run_import` may internally
            # rollback the session (e.g. on a concurrent-import conflict),
            # which must not wipe out the files already imported earlier in
            # this same batch/request.
            with session.begin_nested():
                vendor = forced_vendor or _get_or_create_vendor(
                    _vendor_name_from_filename(file_name), session
                )
                saved_path = _save_upload(upload, vendor.name)

                result = import_service.run_import(vendor.id, saved_path, session)

            if result.status == ImportStatus.AWAITING_CONFIRMATION:
                # Mirrors inventory_import.py's default behavior: an
                # unchanged re-upload is treated as a no-op skip, not an
                # error requiring the admin to confirm/cancel by hand.
                import_service.cancel_import(result.import_id, session)
                outcomes.append(
                    UploadOutcome(
                        file_name=file_name,
                        vendor_id=vendor.id,
                        vendor_name=vendor.name,
                        import_id=result.import_id,
                        status="SKIPPED_DUPLICATE",
                        is_duplicate=True,
                        row_count=0,
                        error_count=0,
                        message=(
                            f"Unchanged since import #{result.duplicate_of_import_id}; skipped."
                        ),
                    )
                )
                continue

            outcomes.append(
                UploadOutcome(
                    file_name=file_name,
                    vendor_id=vendor.id,
                    vendor_name=vendor.name,
                    import_id=result.import_id,
                    status=result.status.value,
                    is_duplicate=result.is_duplicate,
                    row_count=result.row_count,
                    error_count=result.error_count,
                    message=result.message,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
            logger.exception("Failed to import uploaded file '%s'", file_name)
            outcomes.append(
                UploadOutcome(
                    file_name=file_name,
                    vendor_id=forced_vendor.id if forced_vendor else None,
                    vendor_name=forced_vendor.name if forced_vendor else None,
                    import_id=None,
                    status="FAILED",
                    is_duplicate=False,
                    row_count=0,
                    error_count=0,
                    message=None,
                    error=str(exc),
                )
            )
        finally:
            upload.file.close()

    return outcomes
