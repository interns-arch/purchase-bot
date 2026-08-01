"""User account logic. Kept separate from `core/services/*` on purpose --
this is authentication, not business logic, per the project's architecture
rules."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth.models import RevokedToken, User
from backend.app.auth.security import hash_password, verify_password


def get_user_by_username(username: str, session: Session) -> User | None:
    return session.execute(
        select(User).where(User.username == username.strip().lower())
    ).scalar_one_or_none()


def create_user(
    username: str, password: str, session: Session, *, role: str = "admin"
) -> User:
    username = username.strip().lower()
    if not username:
        raise ValueError("Username cannot be blank.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if get_user_by_username(username, session) is not None:
        raise ValueError(f"A user named '{username}' already exists.")

    user = User(username=username, password_hash=hash_password(password), role=role)
    session.add(user)
    session.flush()
    return user


def authenticate_user(username: str, password: str, session: Session) -> User | None:
    user = get_user_by_username(username, session)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def change_password(
    user: User, current_password: str, new_password: str, session: Session
) -> None:
    if not verify_password(current_password, user.password_hash):
        raise ValueError("Current password is incorrect.")
    if len(new_password) < 8:
        raise ValueError("New password must be at least 8 characters.")
    user.password_hash = hash_password(new_password)
    session.flush()


def revoke_token(jti: str, expires_at: datetime, session: Session) -> None:
    if session.get(RevokedToken, jti) is not None:
        return
    session.add(RevokedToken(jti=jti, expires_at=expires_at))
    session.flush()


def is_token_revoked(jti: str, session: Session) -> bool:
    return session.get(RevokedToken, jti) is not None


def purge_expired_revoked_tokens(session: Session) -> int:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expired = list(
        session.execute(select(RevokedToken).where(RevokedToken.expires_at < now)).scalars()
    )
    for row in expired:
        session.delete(row)
    session.flush()
    return len(expired)
