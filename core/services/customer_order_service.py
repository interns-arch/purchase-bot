"""Orchestrates the customer-order-import workflow: validate an uploaded
order file and store it as `CustomerOrder` + `CustomerOrderItem` rows, so it
becomes durable input the Vendor Comparison matching engine
(`vendor_comparison_service.compare_vendors`) can run against on demand --
instead of the old CLI behavior of re-reading `input.csv` from disk on every
run.

Deliberately mirrors `delivery_import_service.py`'s shape (same dedupe-by-
hash pattern, same per-row error logging) for consistency with the rest of
`core/services/`. Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.hashing import sha256_of_file
from core.ingestion.column_detector import (
    PART_NUMBER_HEADERS,
    QUANTITY_HEADERS,
    detect_header_row,
    find_optional_column,
    is_parseable_quantity,
    parse_quantity,
)
from core.ingestion.csv_reader import read_csv_grid
from core.ingestion.excel_reader import read_excel_grid
from core.models import CustomerOrder, CustomerOrderImportError, CustomerOrderItem, CustomerOrderStatus

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}


class DuplicateCustomerOrderFileError(Exception):
    """Raised when this exact file (name + content) was already imported."""

    def __init__(self, existing_order_id: int):
        self.existing_order_id = existing_order_id
        super().__init__(
            f"This file's content was already imported as customer order #{existing_order_id}."
        )


class CustomerOrderQuantityMissingError(Exception):
    """The line-item table was located but has NO requested-quantity column.
    We never invent a quantity from other fields (PO ID, totals, price, ...);
    the caller reports this as NEEDS_REVIEW instead."""

    def __init__(self, file_name: str, headers: list[str]):
        self.file_name = file_name
        self.headers = headers
        super().__init__(
            f"Requested quantity column is missing in '{file_name}'. Line-item headers "
            f"found: {headers}. A requested-quantity column is required -- please add one "
            "(the order was not imported to avoid inventing quantities)."
        )


@dataclass
class CustomerOrderImportResult:
    order_id: int
    file_name: str
    status: CustomerOrderStatus
    row_count: int
    error_count: int
    errors: list[str] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _read_grid(file_path: Path) -> list[list[str]]:
    if file_path.suffix.lower() == ".csv":
        return read_csv_grid(file_path)
    return read_excel_grid(file_path)


def _parse_line_items(file_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Locate the line-item header row ANYWHERE in the worksheet (metadata
    rows above it are ignored) and return (headers, item_rows).

    Works for both a simple single-section order (header on row 1 -> detected
    at index 0) and a multi-section order (metadata block, then the real
    `... PART NUMBER ...` header, then the item rows). The item table ends at
    the first fully-blank row (a section boundary)."""
    grid = _read_grid(file_path)
    header_index = detect_header_row(grid, PART_NUMBER_HEADERS)
    if header_index is None:
        raise ValueError(
            f"Part-number column not found in '{file_path.name}'. "
            f"No line-item header row was detected."
        )

    header_row = grid[header_index]
    column_count = 0
    for index, value in enumerate(header_row):
        if str(value).strip():
            column_count = index + 1
    headers = [str(header_row[i]).strip() for i in range(column_count)]

    item_rows: list[dict[str, str]] = []
    for data_row in grid[header_index + 1 :]:
        padded = list(data_row) + [""] * max(0, column_count - len(data_row))
        cells = [str(padded[i]).strip() for i in range(column_count)]
        if not any(cells):
            break  # blank row -> end of the line-item section
        row_dict = {headers[i]: cells[i] for i in range(column_count)}
        item_rows.append(row_dict)

    return headers, item_rows


def run_customer_order_import(
    file_path: Path, session: Session, *, customer_id: int | None = None
) -> CustomerOrderImportResult:
    """Import one customer order file. Raises `DuplicateCustomerOrderFileError`
    if this exact file content was already imported successfully.

    `customer_id` associates the resulting `CustomerOrder` with the customer
    it was resolved to belong to (see
    `document_processor.detector._classify_customer_order`); `None` for
    channels (Gmail, manual upload) that don't identify a customer, exactly
    as before this parameter existed."""
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file extension '{file_path.suffix}'. "
            f"Supported: {sorted(SUPPORTED_EXTENSIONS)}."
        )

    file_size = file_path.stat().st_size
    if file_size == 0:
        raise ValueError(f"'{file_path.name}' is empty.")

    content_hash = sha256_of_file(file_path)

    existing = session.execute(
        select(CustomerOrder).where(
            CustomerOrder.file_name == file_path.name,
            CustomerOrder.content_hash == content_hash,
            CustomerOrder.status.in_(
                (CustomerOrderStatus.COMPLETED, CustomerOrderStatus.COMPLETED_WITH_ERRORS)
            ),
        )
    ).scalar_one_or_none()

    if existing is not None:
        raise DuplicateCustomerOrderFileError(existing.id)

    headers, item_rows = _parse_line_items(file_path)
    part_column = find_optional_column(headers, PART_NUMBER_HEADERS)
    if part_column is None:
        raise ValueError(
            f"Part-number column not found in '{file_path.name}'. Headers found: {headers}"
        )
    # No quantity column -> do NOT invent one from PO ID / totals / price /
    # etc. Signal NEEDS_REVIEW (the dispatcher maps this, no order is stored).
    quantity_column = find_optional_column(headers, QUANTITY_HEADERS)
    if quantity_column is None:
        raise CustomerOrderQuantityMissingError(file_path.name, headers)

    customer_order = CustomerOrder(
        file_name=file_path.name,
        file_size_bytes=file_size,
        content_hash=content_hash,
        status=CustomerOrderStatus.COMPLETED,
        row_count=0,
        error_count=0,
        customer_id=customer_id,
    )
    session.add(customer_order)
    session.flush()  # assign customer_order.id

    row_count = 0
    error_count = 0
    error_messages: list[str] = []

    def _log_error(row_number: int, row: dict, reason: str, detail: str) -> None:
        nonlocal error_count
        session.add(
            CustomerOrderImportError(
                customer_order_id=customer_order.id,
                row_number=row_number,
                raw_row=row,
                error_reason=reason,
                error_detail=detail,
            )
        )
        error_count += 1
        error_messages.append(f"Row {row_number}: {reason} -- {detail}")

    for row_number, row in enumerate(item_rows, start=2):
        raw_part_number = row.get(part_column, "").strip()
        raw_quantity = row.get(quantity_column, "")

        if not raw_part_number:
            _log_error(row_number, row, "INVALID_PART_NUMBER", "Part number is blank.")
            continue

        if not is_parseable_quantity(raw_quantity):
            _log_error(
                row_number, row, "INVALID_QUANTITY", f"Could not parse quantity {raw_quantity!r}."
            )
            continue

        quantity_requested = parse_quantity(raw_quantity)
        if quantity_requested <= 0:
            _log_error(row_number, row, "INVALID_QUANTITY", "Requested quantity must be greater than 0.")
            continue

        session.add(
            CustomerOrderItem(
                customer_order_id=customer_order.id,
                row_number=row_number,
                part_number_raw=raw_part_number,
                quantity_requested=quantity_requested,
                raw_data=row,
            )
        )
        row_count += 1

    customer_order.row_count = row_count
    customer_order.error_count = error_count
    customer_order.status = (
        CustomerOrderStatus.FAILED
        if row_count == 0 and error_count > 0
        else CustomerOrderStatus.COMPLETED_WITH_ERRORS
        if error_count > 0
        else CustomerOrderStatus.COMPLETED
    )
    customer_order.completed_at = _utcnow()
    session.flush()

    return CustomerOrderImportResult(
        order_id=customer_order.id,
        file_name=file_path.name,
        status=customer_order.status,
        row_count=row_count,
        error_count=error_count,
        errors=error_messages,
    )


def get_customer_order(order_id: int, session: Session) -> CustomerOrder | None:
    return session.get(CustomerOrder, order_id)


def list_customer_order_history(session: Session, *, limit: int | None = None) -> list[CustomerOrder]:
    statement = select(CustomerOrder).order_by(CustomerOrder.created_at.desc())
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.execute(statement).scalars())


def list_customer_order_items(order_id: int, session: Session) -> list[CustomerOrderItem]:
    return list(
        session.execute(
            select(CustomerOrderItem)
            .where(CustomerOrderItem.customer_order_id == order_id)
            .order_by(CustomerOrderItem.row_number)
        ).scalars()
    )


def list_customer_order_errors(order_id: int, session: Session) -> list[CustomerOrderImportError]:
    return list(
        session.execute(
            select(CustomerOrderImportError)
            .where(CustomerOrderImportError.customer_order_id == order_id)
            .order_by(CustomerOrderImportError.id)
        ).scalars()
    )
