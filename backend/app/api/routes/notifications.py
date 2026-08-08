"""Live integration-event toasts, polled by the logged-in frontend.

A single tiny GET endpoint over the in-memory `broker` -- no DB, no history,
no activity feed. The client passes the last event id it has seen and gets
back only newer ones (plus the current cursor). On first load it omits
`after` to just fetch the cursor, so old buffered events aren't replayed."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.app.auth.dependencies import get_current_user
from backend.app.notifications import broker

router = APIRouter(
    prefix="/api/notifications",
    tags=["notifications"],
    dependencies=[Depends(get_current_user)],
)


@router.get("")
def poll_notifications(after: int | None = None) -> dict:
    if after is None:
        # Initialisation: hand back the current cursor, no (old) events.
        return {"events": [], "latest_id": broker.latest_id()}
    events = broker.get_since(after)
    return {"events": events, "latest_id": broker.latest_id()}
