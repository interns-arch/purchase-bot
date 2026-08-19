from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ImportStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


# Statuses that hold the "one running import per vendor" lock.
RUNNING_IMPORT_STATUSES = (
    ImportStatus.PENDING,
    ImportStatus.PROCESSING,
    ImportStatus.AWAITING_CONFIRMATION,
)


class DeliveryImportStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class CustomerOrderStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class VendorSelectionStatus(str, enum.Enum):
    SELECTED = "SELECTED"
    ORDERED = "ORDERED"


class VendorDeliveryImportSource(str, enum.Enum):
    FILE_UPLOAD = "FILE_UPLOAD"
    VENDOR_INVOICE_PDF = "VENDOR_INVOICE_PDF"


class InvoiceVerificationStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class InvoiceLineDiscrepancyType(str, enum.Enum):
    MATCHED = "MATCHED"
    SHORT_SUPPLY = "SHORT_SUPPLY"
    EXTRA_SUPPLY = "EXTRA_SUPPLY"
    MISSING_PART = "MISSING_PART"
    UNEXPECTED_PART = "UNEXPECTED_PART"


class VendorPurchaseOrderStatus(str, enum.Enum):
    GENERATED = "GENERATED"
    EMAILED = "EMAILED"
    EMAIL_FAILED = "EMAIL_FAILED"


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    # Permanent identifier used throughout the app for inventory imports
    # (see core.services.vendor_code_service) -- <first two letters of
    # name>_CT, e.g. "AR_CT", with a numeric suffix on collision. NOT the
    # same thing as `whatsapp_number` below: multiple vendors share a single
    # WhatsApp Business number, so the sender's phone number can never
    # identify which vendor a file came from -- only the vendor code
    # embedded in the filename can.
    vendor_code: Mapped[str | None] = mapped_column(default=None)
    # True for the company's own warehouse/dark-store "vendor" rows (e.g.
    # Brijwasan / Company Warehouse) -- see core.services.rules.engine,
    # which always tries these offers before any external vendor.
    is_own_stock: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    contact_info: Mapped[str | None] = mapped_column(default=None)
    payment_terms: Mapped[str | None] = mapped_column(default=None)
    # No longer used to identify which vendor sent an inbound file (see
    # `vendor_code` above) -- kept only as informational contact metadata.
    whatsapp_number: Mapped[str | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ux_vendors_name_lower", func.lower(name), unique=True),
        Index(
            "ux_vendors_whatsapp_number",
            whatsapp_number,
            unique=True,
            sqlite_where=text("whatsapp_number IS NOT NULL"),
            postgresql_where=text("whatsapp_number IS NOT NULL"),
        ),
        Index(
            "ux_vendors_vendor_code",
            vendor_code,
            unique=True,
            sqlite_where=text("vendor_code IS NOT NULL"),
            postgresql_where=text("vendor_code IS NOT NULL"),
        ),
    )


class Part(Base):
    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_part_number: Mapped[str] = mapped_column(nullable=False, unique=True)
    brand: Mapped[str | None] = mapped_column(default=None)
    description: Mapped[str | None] = mapped_column(default=None)
    category: Mapped[str | None] = mapped_column(default=None)
    uom: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class LearnedFileFormat(Base):
    """A vendor-file column format the system has LEARNED (usually from one
    successful AI rescue) or that was defined manually. Keyed by the header
    row's fingerprint (normalized non-empty header cells, in order), so the
    NEXT file with the same header layout maps deterministically with ZERO
    model calls -- the LLM analyses a given format at most once."""

    __tablename__ = "learned_file_formats"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "|"-joined normalise_header() of the header row's non-empty cells.
    header_fingerprint: Mapped[str] = mapped_column(unique=True, index=True)
    # Exact source header texts to map (as read_table_with_mapping expects).
    part_column: Mapped[str] = mapped_column(nullable=False)
    quantity_column: Mapped[str] = mapped_column(nullable=False)
    # Where the mapping came from: "ai:<provider>/<model>" or "manual".
    learned_from: Mapped[str] = mapped_column(default="manual")
    sample_headers: Mapped[dict] = mapped_column(JSON, nullable=False)
    use_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)


class PartAlias(Base):
    __tablename__ = "part_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_part_number: Mapped[str] = mapped_column(nullable=False)
    normalized_part_number: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    part: Mapped[Part] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "vendor_id", "normalized_part_number", name="ux_part_aliases_vendor_normalized"
        ),
        Index("ix_part_aliases_part_id", "part_id"),
    )


class InventoryImport(Base):
    __tablename__ = "inventory_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, name="import_status"),
        default=ImportStatus.PENDING,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    row_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    sheet_name: Mapped[str | None] = mapped_column(default=None)
    duplicate_of_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_imports.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    vendor: Mapped[Vendor] = relationship()

    __table_args__ = (
        Index(
            "ux_inventory_imports_one_active_per_vendor",
            "vendor_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active = true"),
        ),
        Index(
            "ux_inventory_imports_one_running_per_vendor",
            "vendor_id",
            unique=True,
            sqlite_where=text(
                "status IN ('PENDING','PROCESSING','AWAITING_CONFIRMATION')"
            ),
            postgresql_where=text(
                "status IN ('PENDING','PROCESSING','AWAITING_CONFIRMATION')"
            ),
        ),
        Index("ix_inventory_imports_vendor_created", "vendor_id", "created_at"),
        Index("ix_inventory_imports_vendor_hash", "vendor_id", "content_hash"),
    )


class VendorInventory(Base):
    __tablename__ = "vendor_inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    import_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_imports.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("parts.id", ondelete="SET NULL"), default=None
    )
    row_number: Mapped[int] = mapped_column(nullable=False)
    vendor_part_number: Mapped[str] = mapped_column(nullable=False)
    normalized_part_number: Mapped[str] = mapped_column(nullable=False)
    quantity_available: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vendor: Mapped[Vendor] = relationship()
    part: Mapped[Part | None] = relationship()

    __table_args__ = (
        CheckConstraint("quantity_available >= 0", name="ck_vendor_inventory_qty_nonneg"),
        Index("ix_vendor_inventory_import_id", "import_id"),
        Index(
            "ix_vendor_inventory_vendor_normalized",
            "vendor_id",
            "normalized_part_number",
        ),
        Index("ix_vendor_inventory_part_id", "part_id"),
    )


class ImportErrorRecord(Base):
    """Row- or file-level error logged during an inventory import (table: import_errors)."""

    __tablename__ = "import_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_imports.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int | None] = mapped_column(default=None)
    raw_row: Mapped[dict | None] = mapped_column(JSON, default=None)
    error_reason: Mapped[str] = mapped_column(nullable=False)
    error_detail: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_import_errors_import_id", "import_id"),)


class PurchaseOrder(Base):
    """A vendor-specific purchase order, grouping the order lines a single
    vendor was allocated during order matching (one PO per vendor per
    matching run -- see `po_generator.py` / `purchase_order_service.py`)."""

    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(nullable=False, unique=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    source_file: Mapped[str | None] = mapped_column(default=None)
    source_content_hash: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vendor: Mapped[Vendor] = relationship()

    __table_args__ = (
        Index("ix_purchase_orders_vendor_id", "vendor_id"),
        Index("ix_purchase_orders_source_hash", "source_content_hash"),
    )


class PurchaseOrderItem(Base):
    """One ordered part on a `PurchaseOrder` (quantity_ordered = the quantity
    that vendor was allocated to supply for this part)."""

    __tablename__ = "purchase_order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_part_number: Mapped[str] = mapped_column(nullable=False)
    quantity_ordered: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    purchase_order: Mapped[PurchaseOrder] = relationship()
    part: Mapped[Part] = relationship()

    __table_args__ = (
        CheckConstraint("quantity_ordered > 0", name="ck_po_items_qty_positive"),
        UniqueConstraint("po_id", "part_id", name="ux_po_items_po_part"),
        Index("ix_po_items_part_id", "part_id"),
    )


class VendorSelection(Base):
    """The purchase team's (or rule engine's) choice of vendor + quantity for
    one `CustomerOrderItem` (Module 2.5 -- Vendor Selection). An order line
    can now have MULTIPLE selections -- one per vendor -- so a requested
    quantity that no single vendor can cover can be split across several
    (unique on `(customer_order_item_id, vendor_id)`, not on
    `customer_order_item_id` alone; this superseded the original
    single-vendor-per-line decision in ARCHITECTURE.md once the business
    confirmed combination-of-vendors fulfillment is the real workflow --
    see `backend/scripts/migrate_vendor_selection_multi_vendor.py` for the
    migration off the old single-column-unique schema). `status` starts
    `SELECTED` and flips to `ORDERED` -- with `purchase_order_item_id` set --
    once `purchase_order_service.generate_purchase_orders_from_selections`
    turns it into a real PO line; selections are immutable after that."""

    __tablename__ = "vendor_selections"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("customer_order_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_part_number: Mapped[str] = mapped_column(nullable=False)
    quantity_selected: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    status: Mapped[VendorSelectionStatus] = mapped_column(
        Enum(VendorSelectionStatus, name="vendor_selection_status"),
        default=VendorSelectionStatus.SELECTED,
        nullable=False,
    )
    purchase_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_order_items.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    vendor: Mapped[Vendor] = relationship()
    part: Mapped[Part] = relationship()

    __table_args__ = (
        CheckConstraint("quantity_selected > 0", name="ck_vendor_selections_qty_positive"),
        UniqueConstraint(
            "customer_order_item_id", "vendor_id", name="uq_vendor_selection_item_vendor"
        ),
        Index("ix_vendor_selections_vendor_id", "vendor_id"),
        Index("ix_vendor_selections_part_id", "part_id"),
        Index("ix_vendor_selections_order_item_id", "customer_order_item_id"),
        # The reservation ledger's core query (see
        # core.services.vendor_stock_service.reserved_quantity) sums
        # quantity_selected filtered on exactly this pair, across every
        # customer order -- a composite index serves it directly instead of
        # relying on a bitmap-AND of the two single-column indexes above.
        Index("ix_vendor_selections_vendor_part", "vendor_id", "part_id"),
    )


class DeliveryImport(Base):
    """Import-history record for one delivery file scanned from
    `delivery_files/` (mirrors `InventoryImport`, but deliveries are purely
    additive history -- there is no active/superseded concept)."""

    __tablename__ = "delivery_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[DeliveryImportStatus] = mapped_column(
        Enum(DeliveryImportStatus, name="delivery_import_status"), nullable=False
    )
    row_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        # Partial, not a plain UniqueConstraint: `run_delivery_import`'s
        # duplicate check only treats COMPLETED/COMPLETED_WITH_ERRORS as
        # "already imported" -- a FAILED attempt is meant to be retryable
        # (e.g. after fixing the file, or once the PO/vendor data it
        # depends on exists). The constraint must match that, or a retry of
        # a FAILED import raises an unhandled UniqueViolation instead of
        # either succeeding or being cleanly reported as a duplicate.
        Index(
            "ux_delivery_imports_file_hash",
            "file_name",
            "content_hash",
            unique=True,
            sqlite_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
            postgresql_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
        ),
        Index("ix_delivery_imports_content_hash", "content_hash"),
    )


class DeliveryItem(Base):
    """One delivered-quantity line from a delivery file, matched to the
    `PurchaseOrderItem` it fulfills. Multiple `DeliveryItem` rows can point
    at the same PO item (partial/split shipments over time); their
    quantities are summed for gap analysis."""

    __tablename__ = "delivery_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_import_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_imports.id", ondelete="CASCADE"), nullable=False
    )
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    po_item_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(nullable=False)
    po_number_raw: Mapped[str] = mapped_column(nullable=False)
    vendor_part_number: Mapped[str] = mapped_column(nullable=False)
    quantity_delivered: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    delivery_import: Mapped[DeliveryImport] = relationship()
    purchase_order: Mapped[PurchaseOrder] = relationship()
    purchase_order_item: Mapped[PurchaseOrderItem] = relationship()
    vendor: Mapped[Vendor] = relationship()
    part: Mapped[Part] = relationship()

    __table_args__ = (
        CheckConstraint("quantity_delivered >= 0", name="ck_delivery_items_qty_nonneg"),
        Index("ix_delivery_items_delivery_import_id", "delivery_import_id"),
        Index("ix_delivery_items_po_item_id", "po_item_id"),
    )


class DeliveryImportError(Base):
    """Row- or file-level error logged during a delivery import (table:
    delivery_import_errors)."""

    __tablename__ = "delivery_import_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_import_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_imports.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int | None] = mapped_column(default=None)
    raw_row: Mapped[dict | None] = mapped_column(JSON, default=None)
    error_reason: Mapped[str] = mapped_column(nullable=False)
    error_detail: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("ix_delivery_import_errors_import_id", "delivery_import_id"),
    )


class VendorDeliveryImport(Base):
    """Import-history record for one vendor delivery file uploaded through
    the web app's Delivery Tracking module. Deliberately separate from
    `DeliveryImport` above -- that table matches deliveries against
    `PurchaseOrderItem` for the pre-existing CLI pipeline
    (`delivery_import.py`), which stays untouched. Since the web app has no
    Purchase Order concept, deliveries here are matched directly by
    vendor + part number and reconciled against `VendorSelection`
    (see `core/services/vendor_delivery_service.py` and
    `delivery_tracking_service.py`)."""

    __tablename__ = "vendor_delivery_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[DeliveryImportStatus] = mapped_column(
        Enum(DeliveryImportStatus, name="vendor_delivery_import_status"), nullable=False
    )
    # Distinguishes deliveries that arrived as an uploaded delivery file
    # (FILE_UPLOAD, the original/default) from ones written automatically by
    # Vendor Invoice Verification when an invoice PDF's line items are
    # matched/short/extra against `VendorSelection` -- see
    # `core/services/vendor_invoice_verification_service.py`. Existing rows
    # default to FILE_UPLOAD, so nothing about pre-existing data changes.
    source: Mapped[VendorDeliveryImportSource] = mapped_column(
        Enum(VendorDeliveryImportSource, name="vendor_delivery_import_source"),
        default=VendorDeliveryImportSource.FILE_UPLOAD,
        server_default=text("'FILE_UPLOAD'"),
        nullable=False,
    )
    row_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        # Partial, not a plain UniqueConstraint -- same reasoning as
        # `DeliveryImport.ux_delivery_imports_file_hash`: a FAILED import
        # must stay retryable.
        Index(
            "ux_vendor_delivery_imports_file_hash",
            "file_name",
            "content_hash",
            unique=True,
            sqlite_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
            postgresql_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
        ),
        Index("ix_vendor_delivery_imports_content_hash", "content_hash"),
    )


class VendorDeliveryItem(Base):
    """One delivered-quantity line from a vendor delivery file, matched
    directly by vendor + part (no PO). `delivery_date` is read from the
    file when present (see `column_detector.DELIVERY_DATE_HEADERS`),
    otherwise falls back to the import's `created_at` date -- this is what
    powers the Daily Deliveries / Monthly Trend charts and date-range
    filtering on the Delivery Tracking / Vendor Performance dashboards."""

    __tablename__ = "vendor_delivery_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_delivery_import_id: Mapped[int] = mapped_column(
        ForeignKey("vendor_delivery_imports.id", ondelete="CASCADE"), nullable=False
    )
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_part_number: Mapped[str] = mapped_column(nullable=False)
    quantity_delivered: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    delivery_date: Mapped[date | None] = mapped_column(Date, default=None)
    row_number: Mapped[int] = mapped_column(nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vendor_delivery_import: Mapped[VendorDeliveryImport] = relationship()
    vendor: Mapped[Vendor] = relationship()
    part: Mapped[Part] = relationship()

    __table_args__ = (
        CheckConstraint("quantity_delivered >= 0", name="ck_vendor_delivery_items_qty_nonneg"),
        Index("ix_vendor_delivery_items_import_id", "vendor_delivery_import_id"),
        Index("ix_vendor_delivery_items_vendor_id", "vendor_id"),
        Index("ix_vendor_delivery_items_part_id", "part_id"),
        Index("ix_vendor_delivery_items_delivery_date", "delivery_date"),
    )


class VendorInvoiceImport(Base):
    """Import-history + result header for one Vendor Invoice PDF processed
    by `core/services/vendor_invoice_verification_service.py`. `vendor_id`
    is nullable because extraction can fail to resolve a vendor at all (an
    unreadable/unrecognized invoice is still recorded, for visibility, with
    `status=NEEDS_REVIEW`)."""

    __tablename__ = "vendor_invoice_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(nullable=False)
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), default=None
    )
    vendor_name_extracted: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[InvoiceVerificationStatus] = mapped_column(
        Enum(InvoiceVerificationStatus, name="invoice_verification_status"), nullable=False
    )
    row_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(default=None)
    # Set once matched/short/extra lines have been mirrored into a
    # `VendorDeliveryItem` batch (source=VENDOR_INVOICE_PDF), so Delivery
    # Tracking / Vendor Performance pick this invoice up automatically.
    vendor_delivery_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendor_delivery_imports.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    vendor: Mapped[Vendor | None] = relationship()

    __table_args__ = (
        Index(
            "ux_vendor_invoice_imports_file_hash",
            "file_name",
            "content_hash",
            unique=True,
            sqlite_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
            postgresql_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
        ),
        Index("ix_vendor_invoice_imports_content_hash", "content_hash"),
    )


class VendorInvoiceLineResult(Base):
    """One extracted line item from a Vendor Invoice PDF, with the
    discrepancy classification against the vendor's current
    `VendorSelection` allocations."""

    __tablename__ = "vendor_invoice_line_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_import_id: Mapped[int] = mapped_column(
        ForeignKey("vendor_invoice_imports.id", ondelete="CASCADE"), nullable=False
    )
    part_number_raw: Mapped[str] = mapped_column(nullable=False)
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("parts.id", ondelete="SET NULL"), default=None
    )
    quantity_invoiced: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    expected_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    discrepancy_type: Mapped[InvoiceLineDiscrepancyType] = mapped_column(
        Enum(InvoiceLineDiscrepancyType, name="invoice_line_discrepancy_type"), nullable=False
    )
    raw_text: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    invoice_import: Mapped[VendorInvoiceImport] = relationship()
    part: Mapped[Part | None] = relationship()

    __table_args__ = (Index("ix_vendor_invoice_line_results_import_id", "invoice_import_id"),)


class VendorDeliveryImportError(Base):
    """Row- or file-level error logged during a vendor delivery import."""

    __tablename__ = "vendor_delivery_import_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_delivery_import_id: Mapped[int] = mapped_column(
        ForeignKey("vendor_delivery_imports.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int | None] = mapped_column(default=None)
    raw_row: Mapped[dict | None] = mapped_column(JSON, default=None)
    error_reason: Mapped[str] = mapped_column(nullable=False)
    error_detail: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("ix_vendor_delivery_import_errors_import_id", "vendor_delivery_import_id"),
    )


class Customer(Base):
    """A business's own customer -- the counterpart to `Vendor`. Identified
    the same way vendors are (see `core.services.customer_code_service`):
    every WhatsApp Customer Order arrives on the same shared WhatsApp
    Business number, so the sender's phone number can never tell customers
    apart -- only the Customer Code embedded in the filename can."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    # Permanent identifier used for Customer Order imports (see
    # core.services.customer_code_service) -- name-derived stem + "_CO",
    # e.g. "AB_CO"; collisions get a longer name-derived stem ("AMA_CO" vs
    # "AMI_CO"), never a blind numeric suffix. Mirrors `Vendor.vendor_code`.
    customer_code: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ux_customers_name_lower", func.lower(name), unique=True),
        Index(
            "ux_customers_customer_code",
            customer_code,
            unique=True,
            sqlite_where=text("customer_code IS NOT NULL"),
            postgresql_where=text("customer_code IS NOT NULL"),
        ),
    )


class CustomerOrder(Base):
    """Import-history record for one customer order file. Mirrors
    `DeliveryImport`: purely additive history -- there is no active/
    superseded concept, since each uploaded order is its own distinct
    customer request, not a replacement of a previous one."""

    __tablename__ = "customer_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[CustomerOrderStatus] = mapped_column(
        Enum(CustomerOrderStatus, name="customer_order_status"), nullable=False
    )
    row_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Which customer this order belongs to -- resolved from the filename's
    # Customer Code for WhatsApp uploads (see
    # `document_processor.detector._classify_customer_order`); left NULL for
    # Gmail/manual uploads exactly as before this column existed, since
    # neither of those channels identifies a customer.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        # Partial, not a plain UniqueConstraint -- same reasoning as
        # `DeliveryImport.ux_delivery_imports_file_hash`: a FAILED import is
        # meant to be retryable per `run_customer_order_import`'s duplicate
        # check, which only treats COMPLETED/COMPLETED_WITH_ERRORS as
        # "already imported".
        Index(
            "ux_customer_orders_file_hash",
            "file_name",
            "content_hash",
            unique=True,
            sqlite_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
            postgresql_where=text("status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')"),
        ),
        Index("ix_customer_orders_content_hash", "content_hash"),
        Index("ix_customer_orders_customer_id", "customer_id"),
    )


class CustomerOrderItem(Base):
    """One requested-part line from an uploaded customer order file -- the
    input to the Vendor Comparison matching engine
    (`core.services.vendor_comparison_service.compare_vendors`)."""

    __tablename__ = "customer_order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_order_id: Mapped[int] = mapped_column(
        ForeignKey("customer_orders.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(nullable=False)
    part_number_raw: Mapped[str] = mapped_column(nullable=False)
    quantity_requested: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "quantity_requested > 0", name="ck_customer_order_items_qty_positive"
        ),
        Index("ix_customer_order_items_order_id", "customer_order_id"),
    )


class CustomerOrderImportError(Base):
    """Row- or file-level error logged during a customer order import."""

    __tablename__ = "customer_order_import_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_order_id: Mapped[int] = mapped_column(
        ForeignKey("customer_orders.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int | None] = mapped_column(default=None)
    raw_row: Mapped[dict | None] = mapped_column(JSON, default=None)
    error_reason: Mapped[str] = mapped_column(nullable=False)
    error_detail: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("ix_customer_order_import_errors_order_id", "customer_order_id"),
    )


class VendorPurchaseOrder(Base):
    """One Purchase Order for one vendor, generated automatically after
    Automatic (or manual) Vendor Selection groups a customer order's
    `VendorSelection` rows by vendor (see
    `core.services.purchase_order_generation_service`). Distinct from the
    legacy CLI's `PurchaseOrder`/`PurchaseOrderItem` above, which stays
    untouched -- those are keyed off the old PO-based matching pipeline,
    not `VendorSelection`.

    Never sent to the vendor -- `status`/`emailed_at` track delivery of the
    generated document to the internal purchase team only (see
    `PURCHASE_TEAM_EMAIL`)."""

    __tablename__ = "vendor_purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(nullable=False, unique=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    customer_order_id: Mapped[int] = mapped_column(
        ForeignKey("customer_orders.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[VendorPurchaseOrderStatus] = mapped_column(
        Enum(VendorPurchaseOrderStatus, name="vendor_purchase_order_status"),
        default=VendorPurchaseOrderStatus.GENERATED,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    emailed_at: Mapped[datetime | None] = mapped_column(default=None)

    vendor: Mapped[Vendor] = relationship()

    __table_args__ = (
        Index("ix_vendor_purchase_orders_vendor_id", "vendor_id"),
        Index("ix_vendor_purchase_orders_customer_order_id", "customer_order_id"),
    )


class VendorPurchaseOrderItem(Base):
    """One ordered part on a `VendorPurchaseOrder`."""

    __tablename__ = "vendor_purchase_order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("vendor_purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False
    )
    vendor_part_number: Mapped[str] = mapped_column(nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    customer_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("customer_order_items.id", ondelete="CASCADE"), nullable=False
    )

    part: Mapped[Part] = relationship()

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_vendor_po_items_qty_positive"),
        Index("ix_vendor_po_items_purchase_order_id", "purchase_order_id"),
    )


class AuditLog(Base):
    """Management audit trail (Founder spec §24): every MANUAL,
    management-impacting action -- who did it, when, what changed, and why
    when a reason was given. Automatic pipeline processing is deliberately
    NOT audited here (Import History / the File Inbox already record it);
    this table is for human overrides: manual vendor selection and
    deselection, data purges, founder contact-registry updates, PO email
    resends. Append-only -- rows are never updated or deleted by the app."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Who: a web username ("admin") or a WhatsApp actor
    # ("founder-whatsapp:9170..."). Free text -- survives user deletion.
    actor: Mapped[str] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(nullable=False)  # e.g. "manual_vendor_select"
    entity_type: Mapped[str] = mapped_column(nullable=False)  # e.g. "vendor_selection"
    entity_id: Mapped[str | None] = mapped_column(default=None)
    previous_value: Mapped[str | None] = mapped_column(Text, default=None)
    new_value: Mapped[str | None] = mapped_column(Text, default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_audit_logs_created_at", "created_at"),)


class PartNumberLink(Base):
    """A FOUNDER-DECLARED equivalence between part numbers that name the same
    physical part but differ in text -- e.g. 'MF390300ML32' and 'MF390300ML'.

    Normal alias discovery cannot see these: `PartAlias` only learns pairs a
    vendor states side by side in one row, and part-number normalisation only
    strips special characters (so a trailing '32' still makes two different
    parts, correctly -- 'ML32' and 'ML33' really are different items). This
    table is the one place where a human says "these two are the same", and
    nothing is ever inferred into it.

    Every number in one equivalence shares a `group_key`, so three or more
    spellings of one part link together and declaring A=B then B=C makes A=C.
    Numbers are stored normalised (`normalise_part_number`)."""

    __tablename__ = "part_number_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    normalized_part_number: Mapped[str] = mapped_column(unique=True, index=True)
    group_key: Mapped[str] = mapped_column(index=True, nullable=False)
    declared_by: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VendorNameAlias(Base):
    """A REMEMBERED alternative name for a vendor (Founder's rule). Created
    automatically when a duplicate vendor is merged: the duplicate's name
    becomes an alias of the keeper, so a future upload captioned with the OLD
    name (e.g. 'BIJWASHAN STOCK' after merging into 'Bijvasan') resolves to
    the same vendor instead of onboarding a new duplicate. Keyed by the
    filler-insensitive normalized name (`vendor_service.normalise_vendor_name`)."""

    __tablename__ = "vendor_name_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    normalized_name: Mapped[str] = mapped_column(unique=True, index=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
