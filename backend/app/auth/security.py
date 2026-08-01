"""Password hashing and JWT helpers. No FastAPI/DB imports here -- pure
crypto utilities so they stay easy to unit test and reuse."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from backend.app.core.config import settings

_BCRYPT_MAX_BYTES = 72  # bcrypt's hard input limit; longer secrets raise ValueError


def _truncate_to_bcrypt_limit(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(_truncate_to_bcrypt_limit(plain_password), bcrypt.gensalt()).decode(
        "utf-8"
    )


def verify_password(plain_password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(_truncate_to_bcrypt_limit(plain_password), password_hash.encode("utf-8"))


def create_access_token(subject: str, *, extra_claims: dict[str, Any] | None = None) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at)."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.jwt_expire_minutes)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expires_at,
        "jti": jti,
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, jti, expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError (or a subclass) on any invalid/expired token."""
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
