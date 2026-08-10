"""Orchestrates the full inventory-import workflow: validate, stage rows,
detect duplicates, and manage the active/superseded/awaiting-confirmation
lifecycle of an `InventoryImport` batch.

Pure business logic -- no `print()`/`input()` here. The CLI layer
(`inventory_import.py`) is the only place that talks to a human; everything
here is meant to be reusable unchanged from a future FastAPI layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from core.hashing import sha256_of_file
from core.ingestion.column_detector import (
    INVENTORY_PART_NUMBER_HEADERS,
    INVENTORY_QUANTITY_HEADERS,
    MRP_HEADERS,
    PRICE_HEADERS,
    detect_header_row,
    find_inventory_columns,
    find_optional_column,
    is_parseable_quantity,
    normalise_header,
    normalise_part_number,
    parse_quantity,
)
from core.ingestion.csv_reader import read_csv_grid, read_csv_rows
from core.ingestion.excel_reader import read_excel_grid, read_excel_rows
from core.ingestion.types import ParsedFile
from core.models import (
    RUNNING_IMPORT_STATUSES,
    ImportErrorRecord,
    ImportStatus,
    InventoryImport,
    Vendor,
    VendorInventory,
)
from core.services.own_stock import is_own_stock_vendor
from core.services.part_resolution_service import resolve_part

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}
DEFAULT_MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB
DEFAULT_MAX_ROW_COUNT = 200_000


class ConcurrentImportError(Exception):
    """Raised when an import is already running for this vendor."""

    def __init__(self, vendor_id: int, running_import: InventoryImport | None):
        self.vendor_id = vendor_id
        self.running_import = running_import

        message = f"An import is already in progress for vendor {vendor_id}"
        if running_import is not None:
            message += (
                f" (import_id={running_import.id}, "
                f"status={running_import.status.value})."
            )
        else:
            message += "."

        super().__init__(message)


class InvalidStateError(Exception):
    """Raised when confirm/cancel is called on an import in the wrong state."""


class _ImportValidationError(Exception):
    """Internal control-flow exception -- never escapes `run_import`."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class ImportResult:
    import_id: int
    vendor_id: int
    status: ImportStatus
    is_duplicate: bool
    row_count: int
    error_count: int
    duplicate_of_import_id: int | None = None
    message: str | None = None


@dataclass
class MasterInventoryVendorEntry:
    vendor_id: int
    vendor_name: str
    vendor_part_number: str
    quantity_available: Decimal
    price: Decimal | None
    mrp: Decimal | None
    raw_data: dict
    inventory_file: str
    # True for the company's own warehouse/dark-store vendor rows -- see
    # core.services.rules.engine, which always tries these offers first.
    is_own_stock: bool = False


@dataclass
class MasterInventoryRow:
    part_id: int
    canonical_part_number: str
    total_quantity_available: Decimal
    part_description: str | None = None
    brand: str | None = None
    vendors: list[MasterInventoryVendorEntry] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _find_active_import(vendor_id: int, session: Session) -> InventoryImport | None:
    return session.execute(
        select(InventoryImport).where(
            InventoryImport.vendor_id == vendor_id,
            InventoryImport.is_active.is_(True),
        )
    ).scalar_one_or_none()


def _find_running_import(vendor_id: int, session: Session) -> InventoryImport | None:
    return session.execute(
        select(InventoryImport).where(
            InventoryImport.vendor_id == vendor_id,
            InventoryImport.status.in_(RUNNING_IMPORT_STATUSES),
        )
    ).scalar_one_or_none()


def _require_vendor(vendor_id: int, session: Session) -> Vendor:
    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise ValueError(f"Vendor {vendor_id} does not exist.")
    return vendor


def _validate_extension_and_size(
    file_path: Path, file_size: int, max_file_size_bytes: int
) -> None:
    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise _ImportValidationError(
            "UNSUPPORTED_FILE_TYPE",
            f"Unsupported file extension '{file_path.suffix}'. "
            f"Supported: {sorted(SUPPORTED_EXTENSIONS)}.",
        )

    if file_size == 0:
        raise _ImportValidationError("EMPTY_FILE", "The uploaded file is empty.")

    if file_size > max_file_size_bytes:
        raise _ImportValidationError(
            "FILE_TOO_LARGE",
            f"File size {file_size} bytes exceeds the "
            f"{max_file_size_bytes}-byte limit.",
        )


def _read_file(file_path: Path) -> ParsedFile:
    if file_path.suffix.lower() == ".csv":
        return read_csv_rows(file_path)
    return read_excel_rows(file_path)


def read_table_with_mapping(
    file_path: Path, column_mapping: dict[str, str]
) -> tuple[list[str], list[dict[str, str]], str, str]:
    """Read the inventory table using an EXPLICIT column mapping (source header
    text for `part_number` and `available_quantity`) instead of alias
    detection. Used by the AI-assisted rescue path: the language model only
    proposes WHICH columns to use -- every value imported still comes straight
    from the file via this deterministic read, never from model output.

    Returns (headers, rows, part_column, quantity_column). Raises ValueError
    when the mapping is unusable (missing keys, forbidden money column, or the
    mapped headers don't exist together on any row)."""
    part_source = (column_mapping.get("part_number") or "").strip()
    quantity_source = (
        column_mapping.get("available_quantity") or column_mapping.get("quantity") or ""
    ).strip()
    if not part_source or not quantity_source:
        raise ValueError("Column mapping must name both part_number and available_quantity.")

    # Defense in depth (the AI validator already enforces this): a money /
    # unconfirmed-stock column may NEVER be used as available quantity.
    from core.services.normalized_validation import FORBIDDEN_QUANTITY_HEADERS

    if normalise_header(quantity_source) in FORBIDDEN_QUANTITY_HEADERS:
        raise ValueError(
            f"Mapped quantity column {quantity_source!r} is a money/float-stock "
            "column and may never be used as available quantity."
        )

    if file_path.suffix.lower() == ".csv":
        grid = read_csv_grid(file_path)
    else:
        grid = read_excel_grid(file_path)

    def _find_cell(row, text):
        target = text.strip().casefold()
        for value in row:
            if str(value).strip().casefold() == target:
                return str(value).strip()
        return None

    header_index = None
    part_header = quantity_header = None
    for index, row in enumerate(grid):
        part_header = _find_cell(row, part_source)
        quantity_header = _find_cell(row, quantity_source)
        if part_header and quantity_header:
            header_index = index
            break
    if header_index is None:
        raise ValueError(
            f"Mapped columns {part_source!r} + {quantity_source!r} were not found "
            f"together on any row of '{file_path.name}'."
        )

    header_row = grid[header_index]
    column_count = 0
    for index, value in enumerate(header_row):
        if str(value).strip():
            column_count = index + 1
    headers = [str(header_row[i]).strip() for i in range(column_count)]

    rows: list[dict[str, str]] = []
    for data_row in grid[header_index + 1 :]:
        padded = list(data_row) + [""] * max(0, column_count - len(data_row))
        cells = [str(padded[i]).strip() for i in range(column_count)]
        if not any(cells):
            break  # blank row -> end of the inventory section
        rows.append({headers[i]: cells[i] for i in range(column_count)})

    return headers, rows, part_header, quantity_header


def _read_inventory_table(file_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Locate the real inventory line-item header row ANYWHERE in the file
    (metadata rows above it are ignored) and return (headers, rows). Used only
    when the ordinary row-1 parse doesn't contain a part-number header -- i.e.
    a metadata-first vendor file (e.g. 'Part search Details' on row 1, the real
    'Part Num | ... | Current St | ...' header on row 3). The item table ends
    at the first fully-blank row."""
    if file_path.suffix.lower() == ".csv":
        grid = read_csv_grid(file_path)
    else:
        grid = read_excel_grid(file_path)

    header_index = detect_header_row(
        grid, INVENTORY_PART_NUMBER_HEADERS, quantity_headers=INVENTORY_QUANTITY_HEADERS
    )
    if header_index is None:
        raise ValueError(
            f"Part-number column not found in '{file_path.name}'. "
            f"No inventory header row was detected."
        )

    header_row = grid[header_index]
    column_count = 0
    for index, value in enumerate(header_row):
        if str(value).strip():
            column_count = index + 1
    headers = [str(header_row[i]).strip() for i in range(column_count)]

    rows: list[dict[str, str]] = []
    for data_row in grid[header_index + 1 :]:
        padded = list(data_row) + [""] * max(0, column_count - len(data_row))
        cells = [str(padded[i]).strip() for i in range(column_count)]
        if not any(cells):
            break  # blank row -> end of the inventory section
        rows.append({headers[i]: cells[i] for i in range(column_count)})

    return headers, rows


def _fail_import(
    import_row: InventoryImport, error: _ImportValidationError, session: Session
) -> ImportResult:
    import_row.status = ImportStatus.FAILED
    import_row.completed_at = _utcnow()
    session.add(
        ImportErrorRecord(
            import_id=import_row.id,
            row_number=None,
            error_reason=error.reason,
            error_detail=error.detail,
        )
    )
    session.flush()
    # Propagate the real reason (not just into the error record) so the caller
    # -- and the WhatsApp/UI toast -- shows it instead of "Unknown error".
    return _to_result(import_row, is_duplicate=False, message=error.detail)


def _activate(
    import_row: InventoryImport,
    previous_active: InventoryImport | None,
    session: Session,
) -> None:
    if previous_active is not None:
        previous_active.is_active = False
        previous_active.status = ImportStatus.SUPERSEDED
        session.flush()  # commit the supersede before activating the new one

    import_row.is_active = True
    import_row.status = (
        ImportStatus.COMPLETED_WITH_ERRORS
        if import_row.error_count
        else ImportStatus.COMPLETED
    )
    import_row.completed_at = _utcnow()
    session.flush()


def _to_result(
    import_row: InventoryImport, *, is_duplicate: bool, message: str | None = None
) -> ImportResult:
    return ImportResult(
        import_id=import_row.id,
        vendor_id=import_row.vendor_id,
        status=import_row.status,
        is_duplicate=is_duplicate,
        row_count=import_row.row_count,
        error_count=import_row.error_count,
        duplicate_of_import_id=import_row.duplicate_of_import_id,
        message=message,
    )


def run_import(
    vendor_id: int,
    file_path: Path,
    session: Session,
    *,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_row_count: int = DEFAULT_MAX_ROW_COUNT,
    column_mapping: dict[str, str] | None = None,
    mapping_note: str | None = None,
) -> ImportResult:
    """Import one vendor file. See module docstring and ARCHITECTURE/plan for the
    full state machine this implements."""
    _require_vendor(vendor_id, session)

    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    file_size = file_path.stat().st_size
    content_hash = sha256_of_file(file_path)

    import_row = InventoryImport(
        vendor_id=vendor_id,
        file_name=file_path.name,
        file_size_bytes=file_size,
        content_hash=content_hash,
        status=ImportStatus.PENDING,
    )
    session.add(import_row)

    try:
        session.flush()  # trips the "one running import per vendor" unique index
    except IntegrityError as exc:
        session.rollback()
        running_import = _find_running_import(vendor_id, session)
        raise ConcurrentImportError(vendor_id, running_import) from exc

    import_row.status = ImportStatus.PROCESSING
    session.flush()

    try:
        _validate_extension_and_size(file_path, file_size, max_file_size_bytes)
        parsed_file = _read_file(file_path)
    except _ImportValidationError as exc:
        return _fail_import(import_row, exc, session)
    except ValueError as exc:
        return _fail_import(import_row, _ImportValidationError("NO_HEADER_ROW", str(exc)), session)

    if parsed_file.used_fallback_encoding:
        session.add(
            ImportErrorRecord(
                import_id=import_row.id,
                row_number=None,
                error_reason="ENCODING_FALLBACK_USED",
                error_detail=f"Fell back to '{parsed_file.encoding}' encoding.",
            )
        )

    for ignored_sheet in parsed_file.ignored_sheets:
        session.add(
            ImportErrorRecord(
                import_id=import_row.id,
                row_number=None,
                raw_row={"sheet_name": ignored_sheet},
                error_reason="EXTRA_SHEET_IGNORED",
                error_detail=f"Sheet '{ignored_sheet}' was not the selected sheet.",
            )
        )

    if parsed_file.sheet_name:
        import_row.sheet_name = parsed_file.sheet_name

    if column_mapping is not None:
        # AI-assisted rescue path: the mapping names WHICH columns to use;
        # everything imported below still comes from the file itself via the
        # deterministic re-read (never from model output). All later guards
        # (row limit, duplicates, blank/negative rows, zero-row failure,
        # supersession) apply identically.
        try:
            headers, rows, part_column, quantity_column = read_table_with_mapping(
                file_path, column_mapping
            )
        except ValueError as exc:
            return _fail_import(
                import_row, _ImportValidationError("AI_MAPPING_INVALID", str(exc)), session
            )
    else:
        # Normal case: the header is on row 1 (full metadata preserved above).
        # If that row has no part-number column, the real inventory header is
        # below a metadata block -- re-parse via header-row detection.
        headers = parsed_file.headers
        rows = parsed_file.rows
        if find_optional_column(headers, INVENTORY_PART_NUMBER_HEADERS) is None:
            try:
                headers, rows = _read_inventory_table(file_path)
            except ValueError as exc:
                return _fail_import(
                    import_row,
                    _ImportValidationError("REQUIRED_COLUMNS_NOT_FOUND", str(exc)),
                    session,
                )

        try:
            part_column, quantity_column = find_inventory_columns(headers, file_path.name)
        except ValueError as exc:
            return _fail_import(
                import_row, _ImportValidationError("REQUIRED_COLUMNS_NOT_FOUND", str(exc)), session
            )

    # Price/MRP are optional -- not every vendor file provides them.
    price_column = find_optional_column(headers, PRICE_HEADERS)
    mrp_column = find_optional_column(headers, MRP_HEADERS)

    if len(rows) > max_row_count:
        return _fail_import(
            import_row,
            _ImportValidationError(
                "ROW_COUNT_EXCEEDS_LIMIT",
                f"{len(rows)} rows exceeds the {max_row_count}-row limit.",
            ),
            session,
        )

    # Duplicate short-circuit -- BEFORE the row loop below.
    #
    # The loop calls `resolve_part()` once per row, and each call is a database
    # round-trip; for a 484-row file against hosted Postgres that measured ~116s.
    # The duplicate decision depends only on this vendor's currently-active
    # import and the file's content hash, both already known here, so an
    # unchanged re-upload no longer pays for a full parse it is about to throw
    # away. Semantics are unchanged: same vendor-scoped comparison, same
    # AWAITING_CONFIRMATION result, same `duplicate_of_import_id` -- only the
    # point at which it is detected moved earlier.
    active_import = _find_active_import(vendor_id, session)
    if active_import is not None and active_import.content_hash == content_hash:
        import_row.status = ImportStatus.AWAITING_CONFIRMATION
        import_row.duplicate_of_import_id = active_import.id
        session.flush()
        return _to_result(
            import_row,
            is_duplicate=True,
            message=(
                f"This file's content matches vendor {vendor_id}'s current "
                f"active import (#{active_import.id}). Confirm to make this "
                f"the new active batch anyway, or cancel to discard it."
            ),
        )

    row_count = 0
    error_count = 0

    for row_number, row in enumerate(rows, start=2):
        raw_part_number = row.get(part_column, "").strip()
        raw_quantity = row.get(quantity_column, "")

        if not raw_part_number:
            session.add(
                ImportErrorRecord(
                    import_id=import_row.id,
                    row_number=row_number,
                    raw_row=row,
                    error_reason="INVALID_PART_NUMBER",
                    error_detail="Part number is blank.",
                )
            )
            error_count += 1
            continue

        if not is_parseable_quantity(raw_quantity):
            session.add(
                ImportErrorRecord(
                    import_id=import_row.id,
                    row_number=row_number,
                    raw_row=row,
                    error_reason="INVALID_QUANTITY",
                    error_detail=f"Could not parse quantity {raw_quantity!r}.",
                )
            )
            error_count += 1
            continue

        quantity = parse_quantity(raw_quantity)
        if quantity < 0:
            # Stock can never be negative (DB enforces it with
            # ck_vendor_inventory_qty_nonneg) -- reject just this row instead
            # of letting the constraint blow up the whole import.
            session.add(
                ImportErrorRecord(
                    import_id=import_row.id,
                    row_number=row_number,
                    raw_row=row,
                    error_reason="INVALID_QUANTITY",
                    error_detail=f"Negative quantity {raw_quantity!r} -- stock cannot be negative.",
                )
            )
            error_count += 1
            continue

        part = resolve_part(vendor_id, raw_part_number, session)

        price = None
        if price_column and is_parseable_quantity(row.get(price_column, "")):
            price = parse_quantity(row.get(price_column, ""))

        mrp = None
        if mrp_column and is_parseable_quantity(row.get(mrp_column, "")):
            mrp = parse_quantity(row.get(mrp_column, ""))

        session.add(
            VendorInventory(
                vendor_id=vendor_id,
                import_id=import_row.id,
                part_id=part.id,
                row_number=row_number,
                vendor_part_number=raw_part_number,
                normalized_part_number=normalise_part_number(raw_part_number),
                quantity_available=quantity,
                price=price,
                mrp=mrp,
                raw_data=row,
            )
        )
        row_count += 1

    import_row.row_count = row_count
    import_row.error_count = error_count
    session.flush()

    if row_count == 0:
        # A header row was detected but NOT ONE valid inventory row was
        # imported (empty data region, or every row rejected). Never record
        # this as a success and never activate it -- activating would
        # silently supersede the vendor's previous good inventory with an
        # empty batch (seen in production: ERP exports whose detected header
        # row had no readable data rows -> COMPLETED rows=0, blank workbook
        # tab). The per-row rejects (if any) stay recorded in Import History.
        return _fail_import(
            import_row,
            _ImportValidationError(
                "NO_DATA_ROWS",
                "A header row was found but no inventory rows could be "
                f"imported ({error_count} row(s) rejected -- see Import "
                "History). Nothing was updated; the vendor's previous "
                "inventory (if any) remains active.",
            ),
            session,
        )

    # `active_import` was already fetched (and the duplicate case already
    # returned) before the row loop above, so this is a genuinely new batch.
    _activate(import_row, active_import, session)
    return _to_result(import_row, is_duplicate=False, message=mapping_note)


def confirm_import(import_id: int, session: Session) -> ImportResult:
    import_row = session.get(InventoryImport, import_id)
    if import_row is None:
        raise LookupError(f"Import {import_id} not found.")

    if import_row.status != ImportStatus.AWAITING_CONFIRMATION:
        raise InvalidStateError(
            f"Import {import_id} is not awaiting confirmation "
            f"(current status={import_row.status.value})."
        )

    previous_active = _find_active_import(import_row.vendor_id, session)
    _activate(import_row, previous_active, session)
    import_row.confirmed_at = _utcnow()
    session.flush()
    return _to_result(import_row, is_duplicate=True)


def cancel_import(import_id: int, session: Session) -> ImportResult:
    import_row = session.get(InventoryImport, import_id)
    if import_row is None:
        raise LookupError(f"Import {import_id} not found.")

    if import_row.status != ImportStatus.AWAITING_CONFIRMATION:
        raise InvalidStateError(
            f"Import {import_id} is not awaiting confirmation "
            f"(current status={import_row.status.value})."
        )

    for row in session.execute(
        select(VendorInventory).where(VendorInventory.import_id == import_id)
    ).scalars():
        session.delete(row)

    for row in session.execute(
        select(ImportErrorRecord).where(ImportErrorRecord.import_id == import_id)
    ).scalars():
        session.delete(row)

    import_row.status = ImportStatus.CANCELLED
    import_row.row_count = 0
    import_row.error_count = 0
    session.flush()
    return _to_result(import_row, is_duplicate=True)


def get_active_inventory(vendor_id: int, session: Session) -> list[VendorInventory]:
    active_import = _find_active_import(vendor_id, session)
    if active_import is None:
        return []

    return list(
        session.execute(
            select(VendorInventory)
            .where(VendorInventory.import_id == active_import.id)
            .order_by(VendorInventory.row_number)
        ).scalars()
    )


def get_master_inventory(session: Session) -> list[MasterInventoryRow]:
    active_import_ids = [
        row.id
        for row in session.execute(
            select(InventoryImport).where(InventoryImport.is_active.is_(True))
        ).scalars()
    ]

    if not active_import_ids:
        return []

    inventory_rows = session.execute(
        select(VendorInventory, InventoryImport)
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        # Eager-load the two relationships the loop below reads. Without this
        # SQLAlchemy lazy-loads `.part` and `.vendor` ONE QUERY PER ROW --
        # measured at 6,869 queries for 6,866 active rows, which is what made
        # the dashboard take ~357s and Vendor Comparison ~5s.
        .options(selectinload(VendorInventory.part), selectinload(VendorInventory.vendor))
        .where(
            VendorInventory.import_id.in_(active_import_ids),
            VendorInventory.part_id.isnot(None),
        )
    ).all()

    grouped: dict[int, MasterInventoryRow] = {}

    for inventory_row, inventory_import in inventory_rows:
        part = inventory_row.part
        vendor = inventory_row.vendor

        if part.id not in grouped:
            grouped[part.id] = MasterInventoryRow(
                part_id=part.id,
                canonical_part_number=part.canonical_part_number,
                total_quantity_available=Decimal("0"),
                part_description=part.description,
                brand=part.brand,
            )

        entry = grouped[part.id]
        entry.total_quantity_available += inventory_row.quantity_available
        entry.vendors.append(
            MasterInventoryVendorEntry(
                vendor_id=vendor.id,
                vendor_name=vendor.name,
                vendor_part_number=inventory_row.vendor_part_number,
                quantity_available=inventory_row.quantity_available,
                price=inventory_row.price,
                mrp=inventory_row.mrp,
                raw_data=inventory_row.raw_data,
                inventory_file=inventory_import.file_name,
                is_own_stock=is_own_stock_vendor(vendor.name, flag=vendor.is_own_stock),
            )
        )

    return sorted(grouped.values(), key=lambda row: row.canonical_part_number)


def list_import_history(vendor_id: int, session: Session) -> list[InventoryImport]:
    return list(
        session.execute(
            select(InventoryImport)
            .where(InventoryImport.vendor_id == vendor_id)
            .order_by(InventoryImport.created_at.desc())
        ).scalars()
    )


def list_all_import_history(session: Session, *, limit: int | None = None) -> list[InventoryImport]:
    """Cross-vendor import history, newest first -- used by the web Inventory
    Import module's "import history" view (the CLI only ever looks at one
    vendor at a time, so this didn't exist before)."""
    statement = select(InventoryImport).order_by(InventoryImport.created_at.desc())
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.execute(statement).scalars())


def list_import_errors(import_id: int, session: Session) -> list[ImportErrorRecord]:
    return list(
        session.execute(
            select(ImportErrorRecord)
            .where(ImportErrorRecord.import_id == import_id)
            .order_by(ImportErrorRecord.id)
        ).scalars()
    )
