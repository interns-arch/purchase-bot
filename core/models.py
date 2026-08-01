from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
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


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    contact_info: Mapped[str | None] = mapped_column(default=None)
    payment_terms: Mapped[str | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ux_vendors_name_lower", func.lower(name), unique=True),
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
    is_active: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
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
        UniqueConstraint(
            "file_name", "content_hash", name="ux_delivery_imports_file_hash"
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
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint(
            "file_name", "content_hash", name="ux_customer_orders_file_hash"
        ),
        Index("ix_customer_orders_content_hash", "content_hash"),
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
