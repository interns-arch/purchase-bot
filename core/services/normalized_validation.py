"""Strict validation of a normalized document BEFORE it may reach any
importer. This is the gate that keeps the database trustworthy when a reading
came from a language model (Architecture V2 §9).

Guarantees enforced here, server-side, regardless of what any model claims:

1. Quantities must be present, numeric and >= 0 (parsed by the existing
   `column_detector.is_parseable_quantity` / `parse_quantity`).
2. ANTI-MIS-MAPPING: a source column whose header normalizes into the MRP,
   price, discount or "float stock" alias sets may NEVER be used as the
   available/requested/supplied quantity -- this is checked against
   `_meta.column_mapping`, so a model that maps "MRP" to quantity is rejected
   even if it is confident.
3. Part numbers must be non-empty after `normalise_part_number`.
4. Duplicate part numbers within one document are reported (caller decides).
5. A confidence floor may be applied.

Identity (vendor/customer) and canonical Part resolution are deliberately NOT
done here -- they belong to the existing exact-match services
(`vendor_code_service`, `vendor_service`, `customer_*`, `part_resolution_service`),
which remain the sole authority. This module never touches the database, so it
stays trivially testable and reusable from any layer.

Pure business logic -- no FastAPI/print()/input()/SDK here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core.ingestion.column_detector import (
    DISCOUNT_HEADERS,
    MRP_HEADERS,
    PRICE_HEADERS,
    is_parseable_quantity,
    normalise_header,
    normalise_part_number,
    parse_quantity,
)

# Headers that must never be interpreted as an available/ordered quantity.
# "floatstock" is excluded on purpose: float stock is not confirmed on-hand
# stock and may only be used if explicitly configured (see plan §9).
FORBIDDEN_QUANTITY_HEADERS: set[str] = (
    MRP_HEADERS | PRICE_HEADERS | DISCOUNT_HEADERS | {"floatstock", "floatstk", "float"}
)

# Quantity fields per document type -- the key we look up in _meta.column_mapping.
_QUANTITY_FIELDS = {
    "vendor_inventory": "available_quantity",
    "customer_order": "quantity_requested",
    "vendor_invoice": "quantity_supplied",
}


@dataclass
class ValidationIssue:
    reason: str
    detail: str
    row_index: int | None = None

    def __str__(self) -> str:  # human-readable, used in NEEDS_REVIEW messages
        where = f"row {self.row_index}: " if self.row_index is not None else ""
        return f"{where}{self.reason} -- {self.detail}"


@dataclass
class ValidatedRow:
    """One accepted row, with the quantity already parsed to Decimal so the
    importer never re-parses model output."""

    part_number: str
    quantity: Decimal
    part_name: str | None = None
    source_index: int = 0


@dataclass
class ValidationResult:
    is_valid: bool
    rows: list[ValidatedRow] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    duplicate_part_numbers: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """One human-readable line for a NEEDS_REVIEW status/toast."""
        if self.is_valid:
            return ""
        return "; ".join(str(issue) for issue in self.issues[:5]) or "Validation failed."


def _quantity_source_column(document, quantity_field: str) -> str | None:
    mapping = getattr(getattr(document, "meta", None), "column_mapping", None) or {}
    return mapping.get(quantity_field)


def _check_quantity_mapping(document, document_type: str) -> ValidationIssue | None:
    """Reject a document whose quantity was read from a price/MRP/discount/
    float-stock column, whatever the model's confidence."""
    quantity_field = _QUANTITY_FIELDS[document_type]
    source_column = _quantity_source_column(document, quantity_field)
    if not source_column:
        return None  # no mapping declared -- row-level checks still apply
    if normalise_header(source_column) in FORBIDDEN_QUANTITY_HEADERS:
        return ValidationIssue(
            "SUSPICIOUS_QUANTITY_MAPPING",
            f"Column {source_column!r} is a price/MRP/discount/float-stock field and "
            f"must never be used as {quantity_field.replace('_', ' ')}.",
        )
    return None


def _row_quantity(raw_quantity) -> tuple[Decimal | None, str | None]:
    if raw_quantity is None or str(raw_quantity).strip() == "":
        return None, "Quantity is missing."
    if not is_parseable_quantity(raw_quantity):
        return None, f"Quantity {raw_quantity!r} is not numeric."
    quantity = parse_quantity(raw_quantity)
    if quantity < 0:
        return None, f"Quantity {quantity} is negative."
    return quantity, None


def validate_normalized_document(
    document,
    *,
    minimum_confidence: float = 0.0,
    require_rows: bool = True,
    max_quantity: Decimal | None = None,
) -> ValidationResult:
    """Validate any normalized document (vendor inventory / customer order /
    vendor invoice). Returns a `ValidationResult`; the caller maps
    `is_valid=False` to NEEDS_REVIEW using `result.reason`."""
    document_type = getattr(document, "document_type", None)
    if document_type not in _QUANTITY_FIELDS:
        return ValidationResult(
            is_valid=False,
            issues=[ValidationIssue("UNSUPPORTED_DOCUMENT_TYPE", f"Got {document_type!r}.")],
        )

    issues: list[ValidationIssue] = []

    confidence = float(getattr(getattr(document, "meta", None), "confidence", 0.0) or 0.0)
    if minimum_confidence and confidence < minimum_confidence:
        issues.append(
            ValidationIssue(
                "LOW_CONFIDENCE",
                f"Extraction confidence {confidence:.2f} is below the required "
                f"{minimum_confidence:.2f}.",
            )
        )

    mapping_issue = _check_quantity_mapping(document, document_type)
    if mapping_issue is not None:
        issues.append(mapping_issue)

    quantity_attribute = _QUANTITY_FIELDS[document_type]
    accepted: list[ValidatedRow] = []
    seen: dict[str, int] = {}
    duplicates: list[str] = []

    for index, row in enumerate(getattr(document, "rows", []) or []):
        raw_part_number = getattr(row, "part_number", "")
        normalized_part = normalise_part_number(raw_part_number)
        if not normalized_part:
            issues.append(
                ValidationIssue("INVALID_PART_NUMBER", "Part number is blank.", row_index=index)
            )
            continue

        quantity, error = _row_quantity(getattr(row, quantity_attribute, None))
        if quantity is None:
            issues.append(ValidationIssue("INVALID_QUANTITY", error or "Invalid.", row_index=index))
            continue

        if max_quantity is not None and quantity > max_quantity:
            issues.append(
                ValidationIssue(
                    "IMPLAUSIBLE_QUANTITY",
                    f"Quantity {quantity} exceeds the configured maximum {max_quantity}.",
                    row_index=index,
                )
            )
            continue

        if normalized_part in seen:
            duplicates.append(normalized_part)
        else:
            seen[normalized_part] = index

        accepted.append(
            ValidatedRow(
                part_number=str(raw_part_number).strip(),
                quantity=quantity,
                part_name=getattr(row, "part_name", None) or getattr(row, "description", None),
                source_index=index,
            )
        )

    if require_rows and not accepted:
        issues.append(
            ValidationIssue("NO_VALID_ROWS", "No row passed part-number/quantity validation.")
        )

    # Blocking issues are anything document-level, or the complete absence of
    # usable rows. Individual bad rows are reported but do not fail the whole
    # document (mirrors the existing importers' per-row error behaviour).
    blocking = [
        issue
        for issue in issues
        if issue.row_index is None
        or issue.reason in {"SUSPICIOUS_QUANTITY_MAPPING", "LOW_CONFIDENCE"}
    ]

    return ValidationResult(
        is_valid=not blocking,
        rows=accepted,
        issues=issues,
        duplicate_part_numbers=sorted(set(duplicates)),
    )
