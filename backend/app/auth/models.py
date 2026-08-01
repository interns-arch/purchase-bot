"""Auth tables. Deliberately separate from `core/models.py` (the business
schema) even though both share the same `Base`/database -- authentication
must stay independent of business logic so it can evolve (more users, roles,
SSO) without touching vendor/inventory code.

`role` exists now purely so a future RBAC layer (Admin / Purchase Team /
Warehouse / Manager) can be introduced by *reading* this column -- no schema
migration needed then. Nothing in the app enforces roles yet; every user is
effectively an admin in Phase 1.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(nullable=False, default="admin")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RevokedToken(Base):
    """JTI blocklist so `/auth/logout` can actually invalidate a token
    server-side instead of relying on the client merely discarding it."""

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
