"""Upload orchestration for Customer Orders. Framework-specific glue only --
saving the uploaded bytes to disk. All actual import logic (column
detection, validation, duplicate handling) lives in
`core.services.customer_order_service.run_customer_order_import`, called
here unchanged.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from core.logging_setup import get_logger
from core.services import customer_order_service as order_service

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
UPLOAD_ROOT = REPO_ROOT / "uploads" / "customer_orders"
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class CustomerOrderUploadOutcome:
    file_name: str
    order_id: int | None
    status: str
    row_count: int
    error_count: int
    message: str | None
    error: str | None = None


def _safe_component(text: str) -> str:
    return _UNSAFE_CHARS.sub("_", text).strip("_") or "file"


def _save_upload(upload: UploadFile) -> Path:
    # The uniquifying timestamp/uuid goes on the DIRECTORY, not the filename
    # itself -- `run_customer_order_import` records `file_path.name` as
    # `CustomerOrder.file_name`, which the dedupe-by-(file_name, hash) check
    # depends on staying the real original name.
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8]
    dest_dir = UPLOAD_ROOT / f"{timestamp}_{unique_suffix}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / _safe_component(upload.filename or "order")

    with dest_path.open("wb") as out_file:
        while chunk := upload.file.read(1024 * 1024):
            out_file.write(chunk)

    return dest_path


def process_customer_order_uploads(
    files: list[UploadFile], session: Session
) -> list[CustomerOrderUploadOutcome]:
    outcomes: list[CustomerOrderUploadOutcome] = []

    for upload in files:
        file_name = upload.filename or "order"
        try:
            with session.begin_nested():
                saved_path = _save_upload(upload)
                result = order_service.run_customer_order_import(saved_path, session)

            outcomes.append(
                CustomerOrderUploadOutcome(
                    file_name=file_name,
                    order_id=result.order_id,
                    status=result.status.value,
                    row_count=result.row_count,
                    error_count=result.error_count,
                    message=None,
                )
            )
        except order_service.DuplicateCustomerOrderFileError as exc:
            outcomes.append(
                CustomerOrderUploadOutcome(
                    file_name=file_name,
                    order_id=exc.existing_order_id,
                    status="SKIPPED_DUPLICATE",
                    row_count=0,
                    error_count=0,
                    message=str(exc),
                )
            )
        except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
            logger.exception("Failed to import customer order file '%s'", file_name)
            outcomes.append(
                CustomerOrderUploadOutcome(
                    file_name=file_name,
                    order_id=None,
                    status="FAILED",
                    row_count=0,
                    error_count=0,
                    message=None,
                    error=str(exc),
                )
            )
        finally:
            upload.file.close()

    return outcomes
