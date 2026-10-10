from __future__ import annotations

import os

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth import service as auth_service
from backend.app.auth.models import User
from backend.app.auth.security import decode_access_token
from backend.app.database.session import get_db

# tokenUrl is the login route the interactive docs (/docs) use to fetch a
# token -- it does not restrict where else a client can send credentials.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


def _auth_disabled() -> bool:
    """AUTH_DISABLED=true (Founder, 10 Oct 2026: "remove login id and
    password"): the website opens without a login and every request acts as
    the first active user. Set it back to false to bring the login back."""
    return (os.environ.get("AUTH_DISABLED") or "").strip().lower() == "true"


def get_current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    if _auth_disabled():
        try:
            return _user_from_token(token, db)
        except HTTPException:
            user = db.execute(
                select(User).where(User.is_active.is_(True)).order_by(User.id).limit(1)
            ).scalar_one_or_none()
            if user is None:
                raise
            return user
    return _user_from_token(token, db)


def _user_from_token(token: str | None, db: Session) -> User:
    if token is None:
        raise _CREDENTIALS_ERROR

    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise _CREDENTIALS_ERROR from None

    username = payload.get("sub")
    jti = payload.get("jti")
    if not username or not jti:
        raise _CREDENTIALS_ERROR

    if auth_service.is_token_revoked(jti, db):
        raise _CREDENTIALS_ERROR

    user = auth_service.get_user_by_username(username, db)
    if user is None or not user.is_active:
        raise _CREDENTIALS_ERROR

    return user
