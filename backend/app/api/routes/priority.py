"""Brand-wise vendor PRIORITY for the shared dashboard (Prateek sir's
dropdowns), used by the purchase bot and readable by the sales bot.

The purchase bot asks a brand's vendors in this order for every advance
order (priority 1 first; the next one only after a "no" or a timeout). The
dashboard reads the list, Prateek sir reorders it with dropdowns, the
dashboard PUTs it back -- the very next advance order uses the new order.
ProcureHub stays the single source of truth, so the bot and the dashboard
can never disagree.

Machine-to-machine: header `X-Api-Key: <DASHBOARD_API_KEY>` (set in
backend/.env). Same data as the logged-in /api/vendor-brands desk routes."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from backend.app.api.routes.advance_orders import list_vendor_brands, put_vendor_brand, VendorBrandIn
from backend.app.database.session import get_db
from core.models import Vendor


def require_dashboard_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = (os.environ.get("DASHBOARD_API_KEY") or "").strip()
    if not expected or not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing X-Api-Key")


router = APIRouter(prefix="/api/priority", tags=["priority"], dependencies=[Depends(require_dashboard_key)])


class PriorityIn(BaseModel):
    vendor_ids: list[int]  # first = asked first


@router.get("/brands")
def brands(db: Session = Depends(get_db)) -> dict[str, list[dict]]:
    """{brand: [{vendor_id, vendor_name, priority, active, discount...}]},
    each brand's vendors in asking order."""
    return list_vendor_brands(db)


@router.put("/brands/{brand}")
def set_brand_priority(brand: str, body: PriorityIn, db: Session = Depends(get_db)) -> dict[str, list[dict]]:
    """Replace one brand's vendor order (the dropdown result). A vendor left
    out is removed from that brand; standing terms of the others are kept."""
    return put_vendor_brand(brand, VendorBrandIn(vendor_ids=body.vendor_ids), db)


@router.get("/vendors")
def vendors(db: Session = Depends(get_db)) -> list[dict]:
    """Every active vendor, for the dropdown options."""
    rows = db.execute(
        select(Vendor.id, Vendor.name, Vendor.vendor_code)
        .where(Vendor.active.is_(True), Vendor.is_own_stock.is_(False))
        .order_by(Vendor.name)
    ).all()
    return [{"vendor_id": i, "vendor_name": n, "vendor_code": c} for i, n, c in rows]


@router.get("/performance")
def performance(db: Session = Depends(get_db)) -> dict:
    """Vendor performance + each brand's vendors ranked best-first."""
    from backend.app.advance_orders.performance import vendor_performance

    return vendor_performance(db)


@router.get("/brands/{brand}")
def brand(brand: str, db: Session = Depends(get_db)) -> list[dict]:
    key = brand.strip().upper()
    data = list_vendor_brands(db)
    if key not in data and not db.execute(select(m.VendorBrand.id).where(m.VendorBrand.brand == key)).first():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="brand not in the vendor list")
    return data.get(key, [])
