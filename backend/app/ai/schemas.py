"""Normalized document schemas -- the single internal representation every
input channel and every parser (deterministic OR model-assisted) converges on.

Field names deliberately mirror the existing database columns / service
signatures (`vendor_part_number`->`part_number`, `quantity_available`,
`quantity_requested`, `quantity_supplied`) so no duplicate business model is
introduced -- see ARCHITECTURE_V2_PLAN.md §8.

Plain dataclasses (not pydantic) so `core.services.normalized_validation` can
consume them without pulling a web dependency into the pure business layer.
Parsing from a raw provider dict is STRICT: unknown shapes raise
`NormalizedSchemaError` rather than being coerced, because a malformed model
response must become NEEDS_REVIEW, never a silent import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VENDOR_INVENTORY = "vendor_inventory"
CUSTOMER_ORDER = "customer_order"
VENDOR_INVOICE = "vendor_invoice"

DOCUMENT_TYPES = (VENDOR_INVENTORY, CUSTOMER_ORDER, VENDOR_INVOICE)


class NormalizedSchemaError(ValueError):
    """Raised when a raw provider payload does not match the expected shape."""


@dataclass
class ExtractionMeta:
    """Diagnostics only -- NEVER business data. Used for shadow-mode
    comparison, cost/quality logging, and the validator's anti-mis-mapping
    check (`column_mapping` tells us which source column the reader believed
    was the quantity)."""

    confidence: float = 0.0
    source_header_row: int | None = None
    column_mapping: dict[str, str] = field(default_factory=dict)
    rejected_columns: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    notes: str | None = None


@dataclass
class VendorInventoryRow:
    part_number: str
    available_quantity: Any  # validated/parsed downstream, never trusted raw
    part_name: str | None = None
    mrp: Any | None = None
    price: Any | None = None


@dataclass
class CustomerOrderRow:
    part_number: str
    quantity_requested: Any
    part_name: str | None = None


@dataclass
class VendorInvoiceRow:
    part_number: str
    quantity_supplied: Any
    description: str | None = None


@dataclass
class NormalizedVendorInventory:
    rows: list[VendorInventoryRow] = field(default_factory=list)
    vendor_name: str | None = None
    vendor_code: str | None = None
    meta: ExtractionMeta = field(default_factory=ExtractionMeta)
    document_type: str = VENDOR_INVENTORY


@dataclass
class NormalizedCustomerOrder:
    rows: list[CustomerOrderRow] = field(default_factory=list)
    customer_name: str | None = None
    customer_code: str | None = None
    order_reference: str | None = None
    meta: ExtractionMeta = field(default_factory=ExtractionMeta)
    document_type: str = CUSTOMER_ORDER


@dataclass
class NormalizedVendorInvoice:
    rows: list[VendorInvoiceRow] = field(default_factory=list)
    invoice_number: str | None = None
    vendor_name: str | None = None
    invoice_date: str | None = None  # ISO 'YYYY-MM-DD'; parsed downstream
    meta: ExtractionMeta = field(default_factory=ExtractionMeta)
    document_type: str = VENDOR_INVOICE


NormalizedDocument = (
    NormalizedVendorInventory | NormalizedCustomerOrder | NormalizedVendorInvoice
)


# --- Intent (natural-language commands; Phase 6, defined now for stability) --

SEND_MATCHING_FILE = "SEND_MATCHING_FILE"
SHOW_VENDOR_PERFORMANCE = "SHOW_VENDOR_PERFORMANCE"
SHOW_PENDING_INVOICES = "SHOW_PENDING_INVOICES"
SHOW_REMAINING_STOCK = "SHOW_REMAINING_STOCK"
UNKNOWN_INTENT = "UNKNOWN"

INTENTS = (
    SEND_MATCHING_FILE,
    SHOW_VENDOR_PERFORMANCE,
    SHOW_PENDING_INVOICES,
    SHOW_REMAINING_STOCK,
    UNKNOWN_INTENT,
)

RECIPIENT_TYPES = ("PURCHASE_TEAM", "FOUNDER")


@dataclass
class Intent:
    """A CLOSED enum of intents. The model may only choose from `INTENTS`; the
    backend validates and executes. The model never receives the ability to
    run arbitrary operations."""

    intent: str = UNKNOWN_INTENT
    customer_code: str | None = None
    customer_name: str | None = None
    vendor_code: str | None = None
    vendor_name: str | None = None
    recipient_type: str | None = None
    confidence: float = 0.0


# --------------------------- strict parsing --------------------------------


def _require_dict(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise NormalizedSchemaError(f"Expected a JSON object, got {type(raw).__name__}.")
    return raw


def _opt_str(raw: dict, key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, (str, int, float)):
        raise NormalizedSchemaError(f"Field {key!r} must be a string, got {type(value).__name__}.")
    text = str(value).strip()
    return text or None


def _rows_of(raw: dict) -> list[dict]:
    rows = raw.get("rows")
    if rows is None:
        raise NormalizedSchemaError("Missing 'rows'.")
    if not isinstance(rows, list):
        raise NormalizedSchemaError(f"'rows' must be a list, got {type(rows).__name__}.")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise NormalizedSchemaError(f"rows[{index}] must be an object.")
    return rows


def _meta_of(raw: dict) -> ExtractionMeta:
    meta_raw = raw.get("_meta") or raw.get("meta") or {}
    if not isinstance(meta_raw, dict):
        return ExtractionMeta()
    try:
        confidence = float(meta_raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    header_row = meta_raw.get("source_header_row")
    mapping = meta_raw.get("column_mapping") or {}
    rejected = meta_raw.get("rejected_columns") or []
    return ExtractionMeta(
        confidence=confidence,
        source_header_row=header_row if isinstance(header_row, int) else None,
        column_mapping={str(k): str(v) for k, v in mapping.items()} if isinstance(mapping, dict) else {},
        rejected_columns=[str(c) for c in rejected] if isinstance(rejected, list) else [],
        provider=_opt_str(meta_raw, "provider"),
        model=_opt_str(meta_raw, "model"),
        notes=_opt_str(meta_raw, "notes"),
    )


def _row_part_number(row: dict) -> str:
    """A missing/blank part number is DATA invalid, not shape invalid -- it is
    returned as "" so `normalized_validation` reports it as a per-row
    INVALID_PART_NUMBER and keeps the rest of the document, exactly as the
    existing deterministic importers do. Only structural problems (not a
    list/object, unknown document_type) raise `NormalizedSchemaError`."""
    value = row.get("part_number")
    return "" if value is None else str(value).strip()


def parse_normalized_document(raw: Any) -> NormalizedDocument:
    """Strictly parse a provider payload into the matching normalized
    dataclass. Raises `NormalizedSchemaError` on any shape problem -- callers
    turn that into NEEDS_REVIEW."""
    data = _require_dict(raw)
    document_type = _opt_str(data, "document_type")
    if document_type not in DOCUMENT_TYPES:
        raise NormalizedSchemaError(
            f"Unsupported document_type {document_type!r}; expected one of {list(DOCUMENT_TYPES)}."
        )

    rows = _rows_of(data)
    meta = _meta_of(data)

    if document_type == VENDOR_INVENTORY:
        return NormalizedVendorInventory(
            vendor_name=_opt_str(data, "vendor_name"),
            vendor_code=_opt_str(data, "vendor_code"),
            meta=meta,
            rows=[
                VendorInventoryRow(
                    part_number=_row_part_number(row),
                    available_quantity=row.get("available_quantity"),
                    part_name=_opt_str(row, "part_name"),
                    mrp=row.get("mrp"),
                    price=row.get("price"),
                )
                for index, row in enumerate(rows)
            ],
        )

    if document_type == CUSTOMER_ORDER:
        return NormalizedCustomerOrder(
            customer_name=_opt_str(data, "customer_name"),
            customer_code=_opt_str(data, "customer_code"),
            order_reference=_opt_str(data, "order_reference"),
            meta=meta,
            rows=[
                CustomerOrderRow(
                    part_number=_row_part_number(row),
                    quantity_requested=row.get("quantity_requested"),
                    part_name=_opt_str(row, "part_name"),
                )
                for index, row in enumerate(rows)
            ],
        )

    return NormalizedVendorInvoice(
        invoice_number=_opt_str(data, "invoice_number"),
        vendor_name=_opt_str(data, "vendor_name"),
        invoice_date=_opt_str(data, "invoice_date"),
        meta=meta,
        rows=[
            VendorInvoiceRow(
                part_number=_row_part_number(row),
                quantity_supplied=row.get("quantity_supplied"),
                description=_opt_str(row, "description"),
            )
            for index, row in enumerate(rows)
        ],
    )


def parse_intent(raw: Any) -> Intent:
    """Strictly parse an intent payload. Anything outside the closed `INTENTS`
    set becomes UNKNOWN rather than an error, so the bot can reply with a
    clarification instead of failing."""
    data = _require_dict(raw)
    name = (_opt_str(data, "intent") or UNKNOWN_INTENT).upper()
    if name not in INTENTS:
        name = UNKNOWN_INTENT

    recipient = (_opt_str(data, "recipient_type") or "").upper() or None
    if recipient is not None and recipient not in RECIPIENT_TYPES:
        recipient = None

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    return Intent(
        intent=name,
        customer_code=_opt_str(data, "customer_code"),
        customer_name=_opt_str(data, "customer_name"),
        vendor_code=_opt_str(data, "vendor_code"),
        vendor_name=_opt_str(data, "vendor_name"),
        recipient_type=recipient,
        confidence=confidence,
    )
