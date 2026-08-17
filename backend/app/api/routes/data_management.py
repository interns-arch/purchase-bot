"""Settings -> Danger Zone: one-click purge of file-derived data.

Thin wrapper over `backend.app.services.data_purge_service` -- scope
validation and the DB transaction live here; the deletion order/semantics
live in the service. Master data (vendors, customers, users, integration
config) is never touched -- see the service docstring."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.models import User
from backend.app.database.session import get_db
from backend.app.services import audit_service
from backend.app.notifications import broker
from backend.app.services import data_purge_service
from core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(get_current_user)],
)

_SCOPE_LABELS = {
    "all": "ALL file data",
    "vendor": "Vendor Inventory files",
    "customer": "Customer Order files",
    "invoice": "Vendor Invoice files",
}


class PurgeRequest(BaseModel):
    scope: str


class PurgeResponse(BaseModel):
    scope: str
    deleted: dict[str, int]
    total_rows: int


@router.post("/purge", response_model=PurgeResponse)
def purge_file_data(
    payload: PurgeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PurgeResponse:
    try:
        deleted = data_purge_service.purge_files(payload.scope, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    audit_service.record(
        db,
        actor=current_user.username,
        action="data_purge",
        entity_type="database",
        entity_id=payload.scope,
        previous_value=deleted,
        new_value=None,
        reason=f"Danger Zone purge, scope={payload.scope}",
    )
    db.commit()

    total = sum(deleted.values())
    label = _SCOPE_LABELS.get(payload.scope, payload.scope)
    logger.info("Purge '%s' committed: %s row(s) across %s table(s).", payload.scope, total, len(deleted))
    broker.publish(
        "warning",
        f"Deleted {label} from the database.",
        f"{total} row(s) removed across {len(deleted)} table(s)."
        if total
        else "Nothing to delete -- already empty.",
    )
    return PurgeResponse(scope=payload.scope, deleted=deleted, total_rows=total)
