"""Advance-order endpoints.

For the sales bot (X-Api-Key = ADVANCE_ORDER_API_KEY, no user login):
    POST /api/advance-orders                  create; vendors are asked
    GET  /api/advance-orders/{id}             status, per-line answer and ETA
    POST /api/advance-orders/{id}/confirm     the customer said yes
    POST /api/advance-orders/{id}/cancel

For the desk (normal login):
    GET  /api/advance-orders                  recent orders
    POST /api/advance-orders/{id}/lines/{line_id}/answer
                                              record a vendor answer taken on the phone
    GET  /api/vendor-brands                   which vendor is asked for which brand
    PUT  /api/vendor-brands/{brand}           replace the vendor list of one brand, in order

Every endpoint answers 503 while ADVANCE_ORDERS_ENABLED is false, except the
vendor-brand list, which can be filled in before switching on."""

from __future__ import annotations

import hmac
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from backend.app.advance_orders import service
from backend.app.advance_orders.config import advance_order_settings
from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from core.models import Vendor


def _require_enabled() -> None:
    if not advance_order_settings.enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="advance orders are switched off")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    _require_enabled()
    expected = advance_order_settings.api_key
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


class CustomerIn(BaseModel):
    portal_id: str | int | None = None
    name: str | None = None
    phone: str | None = None


class LineIn(BaseModel):
    part_number: str
    part_name: str | None = None
    brand: str | None = None
    qty: int = Field(gt=0)


class AdvanceOrderIn(BaseModel):
    external_ref: str | None = None
    source: str | None = "autoflow"
    customer: CustomerIn = CustomerIn()
    requested_by: str | None = None
    needed_by: date | None = None
    lines: list[LineIn] = Field(min_length=1)


class ConfirmIn(BaseModel):
    line_ids: list[int] | None = None


class LineAnswerIn(BaseModel):
    available: bool
    vendor_id: int | None = None
    available_qty: int | None = None
    eta_date: date | None = None


class VendorBrandIn(BaseModel):
    vendor_ids: list[int]


api_router = APIRouter(prefix="/api/advance-orders", tags=["advance-orders"])


def _get(order_id: int, db: Session) -> m.AdvanceOrder:
    order = db.get(m.AdvanceOrder, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="advance order not found")
    return order


@api_router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_api_key)])
def create_advance_order(body: AdvanceOrderIn, db: Session = Depends(get_db)) -> dict:
    payload = body.model_dump()
    payload["needed_by"] = body.needed_by.isoformat() if body.needed_by else None
    try:
        order = service.create_order(payload, db)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    return service.order_out(order, db)


@api_router.get("/{order_id}", dependencies=[Depends(require_api_key)])
def get_advance_order(order_id: int, db: Session = Depends(get_db)) -> dict:
    return service.order_out(_get(order_id, db), db)


@api_router.post("/{order_id}/confirm", dependencies=[Depends(require_api_key)])
def confirm_advance_order(order_id: int, body: ConfirmIn | None = None, db: Session = Depends(get_db)) -> dict:
    order = _get(order_id, db)
    try:
        service.confirm_order(order, (body.line_ids if body else None), db)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return service.order_out(order, db)


@api_router.post("/{order_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_advance_order(order_id: int, db: Session = Depends(get_db)) -> dict:
    order = _get(order_id, db)
    try:
        service.cancel_order(order, db)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return service.order_out(order, db)


desk_router = APIRouter(tags=["advance-orders"], dependencies=[Depends(get_current_user)])


@desk_router.get("/api/advance-orders")
def list_advance_orders(limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    _require_enabled()
    rows = db.execute(select(m.AdvanceOrder).order_by(m.AdvanceOrder.id.desc()).limit(max(1, min(limit, 200)))).scalars()
    return [service.order_out(o, db) for o in rows]


@desk_router.post("/api/advance-orders/{order_id}/lines/{line_id}/answer")
def answer_line(order_id: int, line_id: int, body: LineAnswerIn, db: Session = Depends(get_db)) -> dict:
    _require_enabled()
    order = _get(order_id, db)
    try:
        service.set_line_answer(
            order, line_id, body.available, db, vendor_id=body.vendor_id, available_qty=body.available_qty, eta=body.eta_date
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return service.order_out(order, db)


@desk_router.get("/api/vendor-brands")
def list_vendor_brands(db: Session = Depends(get_db)) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    rows = db.execute(
        select(m.VendorBrand, Vendor.name)
        .join(Vendor, Vendor.id == m.VendorBrand.vendor_id)
        .order_by(m.VendorBrand.brand, m.VendorBrand.priority)
    ).all()
    for vb, name in rows:
        out.setdefault(vb.brand, []).append({"vendor_id": vb.vendor_id, "vendor_name": name, "priority": vb.priority, "active": vb.active})
    return out


@desk_router.put("/api/vendor-brands/{brand}")
def put_vendor_brand(brand: str, body: VendorBrandIn, db: Session = Depends(get_db)) -> dict[str, list[dict]]:
    """Replace the vendor list of one brand; the first id is asked first.
    Brand "*" is the list for parts whose brand has no list of its own."""
    key = brand.strip().upper() or "*"
    known = set(db.execute(select(Vendor.id).where(Vendor.id.in_(body.vendor_ids))).scalars())
    missing = [v for v in body.vendor_ids if v not in known]
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown vendor id(s): {missing}")
    db.execute(delete(m.VendorBrand).where(m.VendorBrand.brand == key))
    seen: set[int] = set()
    for i, vendor_id in enumerate(body.vendor_ids):
        if vendor_id in seen:
            continue
        seen.add(vendor_id)
        db.add(m.VendorBrand(brand=key, vendor_id=vendor_id, priority=i + 1))
    db.commit()
    return list_vendor_brands(db)
