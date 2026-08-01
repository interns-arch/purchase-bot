from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from core.models import Base

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "app.db"


def _normalize_database_url(url: str) -> str:
    """Neon/Render/Heroku-style connection strings use `postgres://`;
    SQLAlchemy 2.x requires `postgresql://`, and we pin the driver to
    psycopg2 explicitly rather than relying on whichever DBAPI happens to be
    importable."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


def make_engine(database_path: Path = DATABASE_PATH):
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        # Hosted Postgres (e.g. Neon): no local file, no SQLite pragma.
        # pool_pre_ping guards against serverless Postgres closing idle
        # connections out from under a pooled connection.
        return create_engine(
            _normalize_database_url(database_url), future=True, pool_pre_ping=True
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a transactional session: commits on success, rolls back on error."""
    init_db()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
