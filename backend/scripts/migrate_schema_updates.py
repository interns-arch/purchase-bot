"""One-time schema migrations that `Base.metadata.create_all` (the only
migration mechanism this project has -- there is no Alembic; see
`core/db.py`) cannot apply on its own, because it only creates tables that
don't exist yet and never alters an existing one. Safe to run repeatedly
(each step checks whether it's already applied) and against either the local
SQLite dev database or a hosted Postgres (`DATABASE_URL`).

Run from the repository root after pulling in the automation changes:

    python -m backend.scripts.migrate_schema_updates

Covers:
1. `vendor_selections`: "at most one vendor per customer order line" (a
   UNIQUE constraint on `customer_order_item_id` alone) -> "one or more
   vendors per line" (UNIQUE on `(customer_order_item_id, vendor_id)`), so a
   requested quantity that no single vendor can cover can be split across
   several (see Automatic Vendor Selection / the rule engine).
2. `vendor_delivery_imports`: adds the `source` column (FILE_UPLOAD vs
   VENDOR_INVOICE_PDF) used to distinguish deliveries recorded by Vendor
   Invoice Verification from ones uploaded as a delivery file.
3. `incoming_documents`: adds `email_message_id` (+ its dedupe index) and
   `invoice_verification_id`, used by Gmail automation and Vendor Invoice
   Verification respectively.
4. Postgres only: adds the `EMAIL` value to the `document_source` native
   enum type (SQLite has no native enum/CHECK for this column, so nothing
   is needed there -- confirmed empirically: SQLAlchemy's `Enum` type only
   adds a CHECK constraint on SQLite when `create_constraint=True`, which
   this project's models never set).
5. `vendors`: adds `vendor_code` (+ its uniqueness index) and `is_own_stock`,
   then backfills a generated code for any existing vendor row that has
   none -- so an existing database never ends up with an unidentifiable
   vendor once WhatsApp-sender-based identification is removed in favor of
   Vendor Codes (see `core/services/vendor_code_service.py`).
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env BEFORE importing core.db (below), so a standalone run of
# this script targets the SAME database the web app does -- the app loads this
# file via backend.app.core.config, but this script doesn't import config, so
# without this line core.db would find no DATABASE_URL in the environment and
# silently fall back to the local SQLite dev database. Real OS environment
# variables (e.g. Render's DATABASE_URL) still take precedence -- load_dotenv
# never overrides an already-set variable -- so this changes nothing about how
# the script behaves on Render.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import inspect, select, text

# `core.db.Base.metadata` only knows about a table once its model class has
# been imported somewhere in the process -- the running FastAPI app imports
# all of these transitively (via its route modules), but this standalone
# script doesn't, so without these explicit imports `init_db()` below would
# silently skip creating `incoming_documents` and the integration status
# tables on a fresh database.
import backend.app.auth.models  # noqa: F401
import backend.app.documents.models  # noqa: F401
import backend.app.integrations.gmail.models  # noqa: F401
import backend.app.integrations.google_sheets.models  # noqa: F401
import backend.app.integrations.whatsapp.models  # noqa: F401
from core.db import engine, init_db
from core.logging_setup import get_logger
from core.models import Vendor
from core.services import vendor_code_service

logger = get_logger(__name__)

VENDOR_SELECTIONS_TABLE = "vendor_selections"
VENDOR_SELECTIONS_OLD_TABLE = "vendor_selections_pre_multi_vendor"

# Column order must match `VendorSelection`'s mapped columns exactly -- this
# is what gets carried over from the old table into the newly (re)created one.
VENDOR_SELECTIONS_COLUMNS = (
    "id",
    "customer_order_item_id",
    "vendor_id",
    "part_id",
    "vendor_part_number",
    "quantity_selected",
    "status",
    "purchase_order_item_id",
    "created_at",
    "updated_at",
)


def _vendor_selections_needs_migration() -> bool:
    inspector = inspect(engine)
    if VENDOR_SELECTIONS_TABLE not in inspector.get_table_names():
        # Brand-new database -- `init_db()` will create the table with the
        # new (correct) constraint directly. Nothing to migrate.
        return False

    for constraint in inspector.get_unique_constraints(VENDOR_SELECTIONS_TABLE):
        if constraint.get("column_names") == ["customer_order_item_id"]:
            return True

    # SQLite sometimes reports a column-level UNIQUE as an index rather than
    # a named unique constraint -- check there too.
    for index in inspector.get_indexes(VENDOR_SELECTIONS_TABLE):
        if index.get("unique") and index.get("column_names") == ["customer_order_item_id"]:
            return True

    return False


def _migrate_vendor_selections_multi_vendor() -> None:
    if not _vendor_selections_needs_migration():
        print(f"'{VENDOR_SELECTIONS_TABLE}' already allows multiple vendors per order line.")
        return

    print(f"Migrating '{VENDOR_SELECTIONS_TABLE}' to allow multiple vendors per order line...")

    inspector = inspect(engine)
    # Both SQLite and Postgres keep a plain (non-unique-constraint) index's
    # NAME as-is after `RENAME TABLE` -- since index names are global, not
    # scoped per table, those names would collide with the same-named
    # indexes `init_db()` is about to create on the fresh `vendor_selections`
    # table below. Drop them from the old (about-to-be-dropped-anyway) table
    # first.
    old_index_names = [
        index["name"]
        for index in inspector.get_indexes(VENDOR_SELECTIONS_TABLE)
        if not index.get("unique")
    ]

    with engine.begin() as connection:
        connection.execute(
            text(f"ALTER TABLE {VENDOR_SELECTIONS_TABLE} RENAME TO {VENDOR_SELECTIONS_OLD_TABLE}")
        )
        for index_name in old_index_names:
            connection.execute(text(f"DROP INDEX {index_name}"))

    # Recreates every table that doesn't yet exist -- since the real table
    # was just renamed out of the way, this creates a fresh
    # `vendor_selections` from the current (already-updated) model metadata,
    # with the new composite unique constraint.
    init_db()

    column_list = ", ".join(VENDOR_SELECTIONS_COLUMNS)
    with engine.begin() as connection:
        row_count = connection.execute(
            text(f"SELECT COUNT(*) FROM {VENDOR_SELECTIONS_OLD_TABLE}")
        ).scalar_one()
        if row_count:
            connection.execute(
                text(
                    f"INSERT INTO {VENDOR_SELECTIONS_TABLE} ({column_list}) "
                    f"SELECT {column_list} FROM {VENDOR_SELECTIONS_OLD_TABLE}"
                )
            )
            if connection.dialect.name == "postgresql":
                # Explicit `id` values were just copied in -- the `id`
                # sequence hasn't advanced to match, so the next INSERT
                # without an explicit id would collide. Bring it in line.
                connection.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{VENDOR_SELECTIONS_TABLE}', 'id'), "
                        f"(SELECT MAX(id) FROM {VENDOR_SELECTIONS_TABLE}))"
                    )
                )
        connection.execute(text(f"DROP TABLE {VENDOR_SELECTIONS_OLD_TABLE}"))

    print(
        f"Migrated {row_count} existing selection(s). "
        f"'{VENDOR_SELECTIONS_TABLE}' now allows multiple vendors per line."
    )


def _migrate_vendor_delivery_imports_source_column() -> None:
    inspector = inspect(engine)
    if "vendor_delivery_imports" not in inspector.get_table_names():
        # Brand-new database -- `init_db()` creates the column directly.
        return

    existing_columns = {col["name"] for col in inspector.get_columns("vendor_delivery_imports")}
    if "source" in existing_columns:
        print("'vendor_delivery_imports.source' already exists.")
        return

    print("Adding 'vendor_delivery_imports.source' column...")
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE vendor_delivery_imports "
                "ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'FILE_UPLOAD'"
            )
        )
    print("Added 'vendor_delivery_imports.source' (existing rows default to FILE_UPLOAD).")


def _migrate_incoming_documents_columns() -> None:
    inspector = inspect(engine)
    if "incoming_documents" not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns("incoming_documents")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("incoming_documents")}

    with engine.begin() as connection:
        if "email_message_id" not in existing_columns:
            print("Adding 'incoming_documents.email_message_id' column...")
            connection.execute(
                text("ALTER TABLE incoming_documents ADD COLUMN email_message_id VARCHAR")
            )
        if "ux_incoming_documents_email_message_id" not in existing_indexes:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX ux_incoming_documents_email_message_id "
                    "ON incoming_documents (email_message_id) "
                    "WHERE email_message_id IS NOT NULL"
                )
            )
        if "invoice_verification_id" not in existing_columns:
            print("Adding 'incoming_documents.invoice_verification_id' column...")
            connection.execute(
                text("ALTER TABLE incoming_documents ADD COLUMN invoice_verification_id INTEGER")
            )

        if connection.dialect.name == "postgresql":
            # Native Postgres enum type -- SQLite has no equivalent CHECK to
            # update (see module docstring).
            connection.execute(text("ALTER TYPE document_source ADD VALUE IF NOT EXISTS 'EMAIL'"))


def _migrate_vendors_columns() -> None:
    inspector = inspect(engine)
    if "vendors" not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns("vendors")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("vendors")}

    with engine.begin() as connection:
        if "vendor_code" not in existing_columns:
            print("Adding 'vendors.vendor_code' column...")
            connection.execute(text("ALTER TABLE vendors ADD COLUMN vendor_code VARCHAR"))
        if "ux_vendors_vendor_code" not in existing_indexes:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX ux_vendors_vendor_code "
                    "ON vendors (vendor_code) WHERE vendor_code IS NOT NULL"
                )
            )
        if "is_own_stock" not in existing_columns:
            print("Adding 'vendors.is_own_stock' column...")
            connection.execute(
                text("ALTER TABLE vendors ADD COLUMN is_own_stock BOOLEAN NOT NULL DEFAULT FALSE")
            )

    # Backfill: any vendor row that predates this change has no code yet --
    # generate one now using the exact same logic new vendors get, so
    # nothing is left unidentifiable.
    from sqlalchemy.orm import Session as _Session

    with _Session(engine) as session:
        codeless_vendors = list(
            session.execute(select(Vendor).where(Vendor.vendor_code.is_(None))).scalars()
        )
        for vendor in codeless_vendors:
            code = vendor_code_service.generate_vendor_code(vendor.name, session)
            vendor.vendor_code = code
            session.flush()
            print(f"Backfilled vendor code for '{vendor.name}': {code}")
        session.commit()


def _require_expected_database(*, allow_sqlite: bool) -> None:
    """Print the resolved target database (password hidden) and refuse to run
    against local SQLite unless explicitly allowed. This is the safety net for
    the exact footgun that shipped the automation columns to the local SQLite
    dev database instead of the production Postgres/Neon one: a standalone run
    with no DATABASE_URL silently falls back to SQLite. Pass `--allow-sqlite`
    to intentionally migrate a local SQLite dev database."""
    url = engine.url
    print(f"Target database: {url.render_as_string(hide_password=True)}")

    if url.get_backend_name() == "sqlite" and not allow_sqlite:
        print(
            "ABORTING: resolved to a local SQLite database, not PostgreSQL/Neon.\n"
            "  DATABASE_URL is not set in the environment or backend/.env.\n"
            "  Set DATABASE_URL to your Neon connection string and re-run, or pass\n"
            "  --allow-sqlite if you really mean to migrate the local SQLite dev DB.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def main() -> int:
    _require_expected_database(allow_sqlite="--allow-sqlite" in sys.argv)
    _migrate_vendor_selections_multi_vendor()
    _migrate_vendor_delivery_imports_source_column()
    _migrate_incoming_documents_columns()
    _migrate_vendors_columns()
    # Creates any brand-new tables introduced alongside these migrations
    # (e.g. vendor_invoice_imports, vendor_invoice_line_results) that don't
    # need a hand-rolled step since `create_all` already handles new tables.
    init_db()
    return 0


if __name__ == "__main__":
    sys.exit(main())
