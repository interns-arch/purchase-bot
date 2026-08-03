"""Application configuration, read from environment variables (optionally
via a `backend/.env` file -- NOT `venv/.env`; the virtualenv directory is
never read by the app).

Kept deliberately tiny (no pydantic-settings dependency) since this is a
single-process, single-admin deployment today. Values are read once at
import time; restart the process to pick up changed env vars.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env explicitly (rather than relying on cwd) so `python -m
# uvicorn backend.app.main:app` works the same whether launched from the
# repo root or from inside backend/. Real OS environment variables still
# take precedence over anything in the file (load_dotenv default).
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _get_secret_key() -> str:
    # JWT_SECRET_KEY is this app's original name; SECRET_KEY is accepted as a
    # fallback alias (same pattern as WHATSAPP_VERIFY_TOKEN below).
    key = os.environ.get("JWT_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if key:
        return key
    # Dev fallback so the app boots without setup. Every restart invalidates
    # existing tokens, which is fine for local development but must be
    # overridden via JWT_SECRET_KEY (or SECRET_KEY) in any shared/production
    # environment.
    return secrets.token_hex(32)


class Settings:
    jwt_secret_key: str = _get_secret_key()
    jwt_algorithm: str = os.environ.get("ALGORITHM", "HS256")
    jwt_expire_minutes: int = int(
        os.environ.get("JWT_EXPIRE_MINUTES") or os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES") or "480"
    )
    cors_origins: list[str] = [
        origin.strip()
        for origin in os.environ.get(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    ]
    # Document Processing Engine folder structure (see
    # `backend/app/services/document_processor/staging.py`): every incoming
    # file (manual upload or WhatsApp download) lands in `temp_upload_dir`,
    # then gets moved to `upload_dir` (successfully processed) or
    # `failed_upload_dir` (failed/needs-review/unsupported).
    # `archive_upload_dir` is created but not actively used yet -- reserved
    # for a future retention/cleanup job.
    upload_dir: str = os.environ.get("UPLOAD_DIR", "uploads/processed")
    temp_upload_dir: str = os.environ.get("TEMP_UPLOAD_DIR", "uploads/incoming")
    archive_upload_dir: str = os.environ.get("ARCHIVE_UPLOAD_DIR", "uploads/archive")
    failed_upload_dir: str = os.environ.get("FAILED_UPLOAD_DIR", "uploads/failed")

    # Optional one-time admin bootstrap (see main.py's startup hook). Both
    # must be set for it to do anything; otherwise use
    # `backend/scripts/create_admin.py` instead. Never overwrites an
    # existing account -- only creates one if zero users exist yet.
    admin_username: str | None = os.environ.get("ADMIN_USERNAME")
    admin_password: str | None = os.environ.get("ADMIN_PASSWORD")


settings = Settings()
