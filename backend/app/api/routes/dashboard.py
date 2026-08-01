from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.schemas.dashboard import DashboardOut
from core.services import dashboard_service

router = APIRouter(
    prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(get_current_user)]
)


@router.get("", response_model=DashboardOut)
def get_dashboard(db: Session = Depends(get_db)) -> DashboardOut:
    return dashboard_service.get_dashboard_summary(db)
