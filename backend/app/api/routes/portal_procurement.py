"""Read-only view of Portal procurement for the console:
GET /api/portal-procurement -> counts by state and the latest PO-MSTs."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.portal_procurement import models  # noqa: F401 -- registers the tables

router = APIRouter(
    prefix="/api/portal-procurement", tags=["portal-procurement"], dependencies=[Depends(get_current_user)]
)


@router.get("/live")
def portal_procurement_live(db: Session = Depends(get_db)) -> dict:
    """Every part on Active Procurement the bot is working on, by brand."""
    from backend.app.portal_procurement import live

    return live.live(db)


@router.get("")
def portal_procurement_summary(db: Session = Depends(get_db)) -> dict:
    from backend.app.portal_procurement import service

    return service.summary(db)
