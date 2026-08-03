"""The only place that writes uploaded/downloaded bytes to disk, for both
manual uploads and WhatsApp downloads. Landing zone is `uploads/incoming/`;
`processor.py` moves the file to `uploads/processed/` or `uploads/failed/`
once it knows the outcome. `uploads/archive/` is created as part of the
folder structure but not actively used this pass -- reserved for a future
retention/cleanup job."""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from backend.app.core.config import settings
from backend.app.documents.models import DocumentSource

REPO_ROOT = Path(__file__).resolve().parents[4]
INCOMING_ROOT = REPO_ROOT / settings.temp_upload_dir
PROCESSED_ROOT = REPO_ROOT / settings.upload_dir
FAILED_ROOT = REPO_ROOT / settings.failed_upload_dir
ARCHIVE_ROOT = REPO_ROOT / settings.archive_upload_dir

for _root in (INCOMING_ROOT, PROCESSED_ROOT, FAILED_ROOT, ARCHIVE_ROOT):
    _root.mkdir(parents=True, exist_ok=True)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(text: str) -> str:
    return _UNSAFE_CHARS.sub("_", text).strip("_") or "file"


def _relative_dest_dir(source: DocumentSource) -> Path:
    # The uniquifying timestamp/uuid goes on the DIRECTORY, not the filename
    # -- downstream `core.services.*` calls record `file_path.name` as the
    # display/dedupe key, so it must stay the real original name.
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8]
    return Path(source.value.lower()) / f"{timestamp}_{unique_suffix}"


def save_incoming_upload(upload: UploadFile, source: DocumentSource) -> Path:
    dest_dir = INCOMING_ROOT / _relative_dest_dir(source)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / _safe_component(upload.filename or "upload")

    with dest_path.open("wb") as out_file:
        while chunk := upload.file.read(1024 * 1024):
            out_file.write(chunk)

    return dest_path


def save_incoming_bytes(data: bytes, filename: str, source: DocumentSource) -> Path:
    dest_dir = INCOMING_ROOT / _relative_dest_dir(source)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / _safe_component(filename or "upload")
    dest_path.write_bytes(data)
    return dest_path


def _move_to(file_path: Path, root: Path) -> None:
    """Best-effort move to a terminal folder. Never raises -- a failed
    housekeeping move must not turn an already-recorded outcome into a
    reported failure."""
    try:
        relative = file_path.relative_to(INCOMING_ROOT)
    except ValueError:
        return  # not under incoming/ (already moved, or a test path) -- nothing to do

    dest_path = root / relative
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(file_path), str(dest_path))
    except OSError:
        pass


def mark_processed_location(file_path: Path) -> None:
    _move_to(file_path, PROCESSED_ROOT)


def mark_failed_location(file_path: Path) -> None:
    _move_to(file_path, FAILED_ROOT)
