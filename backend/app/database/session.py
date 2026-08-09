"""FastAPI database dependency.

This does not introduce a second database or a second engine -- it wraps the
existing `core.db` engine/session (the same `database/app.db` SQLite file the
CLI scripts use) in the generator shape FastAPI's `Depends` expects. All
table metadata (business tables from `core.models` + the `User`/auth tables
from `app.auth.models`) is registered on the same `Base`, so a single
`init_db()` call creates everything.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from core.db import SessionLocal


def get_db() -> Iterator[Session]:
    # `init_db()` is deliberately NOT called here. It runs
    # `Base.metadata.create_all`, which issues ~30 catalogue/reflection queries
    # -- measured at 2.4-3.6s per request against hosted Postgres. Doing that on
    # EVERY request added that cost to every endpoint (a `/api/notifications`
    # poll does no database work of its own, yet took ~3s). Schema creation is a
    # startup concern, so it now happens once in the FastAPI lifespan
    # (`backend/app/main.py`); the CLI keeps its own `init_db()` via
    # `core.db.get_session()`.
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
