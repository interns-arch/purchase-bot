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
    key = os.environ.get("JWT_SECRET_KEY")
    if key:
        return key
    # Dev fallback so the app boots without setup. Every restart invalidates
    # existing tokens, which is fine for local development but must be
    # overridden via JWT_SECRET_KEY in any shared/production environment.
    return secrets.token_hex(32)


class Settings:
    jwt_secret_key: str = _get_secret_key()
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = int(os.environ.get("JWT_EXPIRE_MINUTES", "480"))
    cors_origins: list[str] = [
        origin.strip()
        for origin in os.environ.get(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    ]
    upload_dir: str = os.environ.get("UPLOAD_DIR", "uploads/inventory")

    # Optional one-time admin bootstrap (see main.py's startup hook). Both
    # must be set for it to do anything; otherwise use
    # `backend/scripts/create_admin.py` instead. Never overwrites an
    # existing account -- only creates one if zero users exist yet.
    admin_username: str | None = os.environ.get("ADMIN_USERNAME")
    admin_password: str | None = os.environ.get("ADMIN_PASSWORD")


settings = Settings()
