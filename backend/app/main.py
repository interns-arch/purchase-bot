"""FastAPI application entrypoint.

Run from the repository root so both `core` (the existing business-logic
package) and `backend` are importable:

    python -m uvicorn backend.app.main:app --reload
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from backend.app.api.routes.customer_orders import router as customer_orders_router
from backend.app.api.routes.dashboard import router as dashboard_router
from backend.app.api.routes.deliveries import router as deliveries_router
from backend.app.api.routes.delivery_tracking import router as delivery_tracking_router
from backend.app.api.routes.gmail_integration import router as gmail_integration_router
from backend.app.api.routes.google_sheets_integration import router as google_sheets_integration_router
from backend.app.api.routes.integration_status import router as integration_status_router
from backend.app.api.routes.inventory import router as inventory_router
from backend.app.api.routes.purchase_orders import router as purchase_orders_router
from backend.app.api.routes.vendor_comparison import router as vendor_comparison_router
from backend.app.api.routes.vendor_invoices import router as vendor_invoices_router
from backend.app.api.routes.vendor_performance import router as vendor_performance_router
from backend.app.api.routes.vendor_selection import router as vendor_selection_router
from backend.app.api.routes.notifications import router as notifications_router
from backend.app.api.routes.whatsapp import router as whatsapp_router
from backend.app.auth import service as auth_service
from backend.app.auth.models import User
from backend.app.auth.router import router as auth_router
from backend.app.core.config import settings
from backend.app.workers.scheduler import start_scheduler, stop_scheduler
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)


def _bootstrap_admin_if_configured() -> None:
    """Optional convenience for first-time setup: if `ADMIN_USERNAME` /
    `ADMIN_PASSWORD` are set (via `backend/.env` or the environment) AND no
    user accounts exist yet, create one. Never touches an existing account
    -- to reset a password later, use `backend/scripts/create_admin.py
    --reset` instead."""
    if not settings.admin_username or not settings.admin_password:
        return

    with get_session() as session:
        user_count = session.execute(select(func.count()).select_from(User)).scalar_one()
        if user_count > 0:
            return
        try:
            user = auth_service.create_user(
                settings.admin_username, settings.admin_password, session
            )
        except ValueError as exc:
            logger.warning("Could not bootstrap admin user from ADMIN_USERNAME/ADMIN_PASSWORD: %s", exc)
            return
        logger.info("Bootstrapped initial admin user '%s' from ADMIN_USERNAME/ADMIN_PASSWORD.", user.username)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    logger.info("API starting up (cors_origins=%s)", settings.cors_origins)
    _bootstrap_admin_if_configured()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="Vendor Inventory & Order Fulfillment API",
    version="0.1.0",
    description=(
        "Wraps the existing core/ business services (vendor management, "
        "inventory import) behind a versioned REST API for the React frontend."
    ),
    lifespan=lifespan,
)

if "*" in settings.cors_origins:
    logger.warning(
        "CORS_ORIGINS includes '*' -- do not use a wildcard origin in production."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "%s %s -> %s (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, _exc: Exception) -> JSONResponse:
    """Last-resort handler for anything a route didn't already turn into an
    HTTPException. Logs the full exception server-side; the client only
    ever sees a generic message, never a stack trace."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error."},
    )


app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(inventory_router)
app.include_router(deliveries_router)
app.include_router(delivery_tracking_router)
app.include_router(integration_status_router)
app.include_router(gmail_integration_router)
app.include_router(google_sheets_integration_router)
app.include_router(customer_orders_router)
app.include_router(vendor_comparison_router)
app.include_router(vendor_selection_router)
app.include_router(vendor_performance_router)
app.include_router(vendor_invoices_router)
app.include_router(purchase_orders_router)
app.include_router(whatsapp_router)
app.include_router(notifications_router)


@app.get("/health", tags=["health"])
@app.get("/api/health", tags=["health"])
def health_check() -> dict[str, str]:
    """Used by Render to determine whether the service is up."""
    return {"status": "ok"}
