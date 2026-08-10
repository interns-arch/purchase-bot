"""Vendor Selection endpoints -- lets the user allocate one or more vendors +
quantities per order line from the Vendor Comparison report (splitting a
line across vendors when no single vendor can cover it), run the automatic
vendor-selection rule engine, and export the resulting per-part vendor
allocation as an Excel sheet. Thin wrapper over
`core.services.vendor_selection_service` / `core.services.rules.engine`; no
business rules live here."""

from __future__ import annotations

import io
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.core.config import settings
from backend.app.database.session import get_db
from backend.app.integrations.gmail.mailer import send_email
from backend.app.integrations.whatsapp import allocation_output
from backend.app.schemas.vendor_selection import VendorSelectionIn, VendorSelectionOut
from core.logging_setup import get_logger
from core.time_utils import now_ist
from core.models import VendorSelection
from core.services import customer_order_service, purchase_order_generation_service, vendor_selection_service
from core.services.rules.engine import run_automatic_vendor_selection

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/vendor-selection",
    tags=["vendor-selection"],
    dependencies=[Depends(get_current_user)],
)


def _to_out(selection: VendorSelection) -> VendorSelectionOut:
    return VendorSelectionOut(
        id=selection.id,
        customer_order_item_id=selection.customer_order_item_id,
        vendor_id=selection.vendor_id,
        vendor_name=selection.vendor.name,
        vendor_part_number=selection.vendor_part_number,
        quantity_selected=float(selection.quantity_selected),
    )


@router.get("/{order_id}", response_model=list[VendorSelectionOut])
def list_selections(order_id: int, db: Session = Depends(get_db)) -> list[VendorSelectionOut]:
    selections = vendor_selection_service.list_selections_for_order(order_id, db)
    return [_to_out(selection) for selection in selections]


@router.put(
    "/{order_id}/items/{order_item_id}/vendors/{vendor_id}",
    response_model=VendorSelectionOut,
)
def select_vendor(
    order_id: int,
    order_item_id: int,
    vendor_id: int,
    payload: VendorSelectionIn,
    db: Session = Depends(get_db),
) -> VendorSelectionOut:
    try:
        selection = vendor_selection_service.upsert_selection(
            order_item_id,
            vendor_id,
            Decimal(str(payload.quantity_selected)),
            db,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return _to_out(selection)


@router.delete(
    "/{order_id}/items/{order_item_id}/vendors/{vendor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def deselect_vendor(
    order_id: int,
    order_item_id: int,
    vendor_id: int,
    db: Session = Depends(get_db),
) -> None:
    vendor_selection_service.remove_selection(order_item_id, vendor_id, db)


def _allocation_report_workbook_bytes(order_id: int, db: Session) -> bytes:
    rows = vendor_selection_service.list_selections_for_export(order_id, db)
    workbook = vendor_selection_service.to_export_workbook(rows)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _send_allocation_report_email(order_id: int, db: Session) -> None:
    """Best-effort only -- a mail failure must never fail the auto-select
    API call itself. No-ops entirely if ALLOCATION_REPORT_EMAIL isn't set."""
    if not settings.allocation_report_email:
        return

    order = customer_order_service.get_customer_order(order_id, db)
    selections = vendor_selection_service.list_selections_for_order(order_id, db)

    totals_by_vendor: dict[str, Decimal] = {}
    parts_selected: set[int] = set()
    for selection in selections:
        totals_by_vendor[selection.vendor.name] = (
            totals_by_vendor.get(selection.vendor.name, Decimal(0)) + selection.quantity_selected
        )
        parts_selected.add(selection.customer_order_item_id)

    total_vendors_selected = len(totals_by_vendor)
    total_parts = len(parts_selected)
    summary_lines = "\n".join(
        f"  - {vendor_name}: {quantity}" for vendor_name, quantity in totals_by_vendor.items()
    )

    body = (
        f"Customer Order Number: {order_id}\n"
        f"Order File: {order.file_name if order else '-'}\n"
        f"Date: {now_ist().date().isoformat()}\n"
        f"Total Vendors Selected: {total_vendors_selected}\n"
        f"Total Parts: {total_parts}\n\n"
        f"Summary (vendor -> total quantity allocated):\n{summary_lines or '  (none)'}\n"
    )

    try:
        workbook_bytes = _allocation_report_workbook_bytes(order_id, db)
        send_email(
            settings.allocation_report_email,
            f"Vendor Allocation Report - Customer Order {order_id}",
            body,
            attachments=[(f"vendor_allocation_order_{order_id}.xlsx", workbook_bytes)],
        )
    except Exception:  # noqa: BLE001 -- reporting failure must never fail auto-select
        logger.exception("Failed to send allocation report email for order %s", order_id)


@router.post("/{order_id}/auto-select", response_model=list[VendorSelectionOut])
def auto_select_vendors(
    order_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> list[VendorSelectionOut]:
    """One automatic vendor-selection action -- the system decides whether to
    use a single vendor or combine several to fully cover each line (see
    `core.services.rules.engine`). No strategy choice is exposed."""
    try:
        selections = run_automatic_vendor_selection(order_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    _send_allocation_report_email(order_id, db)
    purchase_order_generation_service.generate_and_email_purchase_orders(order_id, db)

    # Per-order allocation report over WhatsApp (requirement: every customer
    # order produces its own allocation output, generated from the database
    # state AFTER this order's reservation). Runs after the response so the two
    # Graph API calls don't add latency to auto-select; opens its own session
    # and is best-effort -- a delivery failure never affects the allocation.
    background_tasks.add_task(allocation_output.send_allocation_report_to_founder, order_id)

    return [_to_out(selection) for selection in selections]


@router.get("/{order_id}/export")
def export_selected_vendors(order_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    rows = vendor_selection_service.list_selections_for_export(order_id, db)
    workbook = vendor_selection_service.to_export_workbook(rows)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    file_name = f"selected_vendors_order_{order_id}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
