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

from core.db import SessionLocal, init_db


def get_db() -> Iterator[Session]:
    init_db()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
