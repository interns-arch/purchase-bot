"""Vendor Management endpoints. Thin wrapper -- all business rules (duplicate
name checks, blank-name validation, etc.) live in
`core.services.vendor_service`, not here."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.schemas.vendor import VendorCreate, VendorDetail, VendorOut, VendorUpdate
from backend.app.database.session import get_db
from core.services import inventory_import_service, vendor_service

router = APIRouter(
    prefix="/api/vendors", tags=["vendors"], dependencies=[Depends(get_current_user)]
)


@router.get("", response_model=list[VendorOut])
def list_vendors(active_only: bool = False, db: Session = Depends(get_db)) -> list[VendorOut]:
    return vendor_service.list_vendors(db, active_only=active_only)


@router.post("", response_model=VendorOut, status_code=status.HTTP_201_CREATED)
def create_vendor(payload: VendorCreate, db: Session = Depends(get_db)) -> VendorOut:
    try:
        return vendor_service.create_vendor(
            payload.name,
            db,
            contact_info=payload.contact_info,
            payment_terms=payload.payment_terms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/{vendor_id}", response_model=VendorDetail)
def get_vendor(vendor_id: int, db: Session = Depends(get_db)) -> VendorDetail:
    vendor = vendor_service.get_vendor(vendor_id, db)
    if vendor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found.")

    active_inventory = inventory_import_service.get_active_inventory(vendor_id, db)
    total_quantity = sum((row.quantity_available for row in active_inventory), start=0)
    total_parts = len({row.part_id for row in active_inventory if row.part_id is not None})

    history = inventory_import_service.list_import_history(vendor_id, db)
    last_import = history[0] if history else None

    return VendorDetail(
        id=vendor.id,
        name=vendor.name,
        contact_info=vendor.contact_info,
        payment_terms=vendor.payment_terms,
        active=vendor.active,
        created_at=vendor.created_at,
        updated_at=vendor.updated_at,
        total_parts=total_parts,
        total_quantity_available=float(total_quantity),
        last_import_at=last_import.created_at if last_import else None,
        last_import_status=last_import.status.value if last_import else None,
    )


@router.patch("/{vendor_id}", response_model=VendorOut)
def update_vendor(
    vendor_id: int, payload: VendorUpdate, db: Session = Depends(get_db)
) -> VendorOut:
    try:
        return vendor_service.update_vendor(
            vendor_id,
            db,
            name=payload.name,
            contact_info=payload.contact_info,
            payment_terms=payload.payment_terms,
            active=payload.active,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{vendor_id}/disable", response_model=VendorOut)
def disable_vendor(vendor_id: int, db: Session = Depends(get_db)) -> VendorOut:
    try:
        return vendor_service.deactivate_vendor(vendor_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
