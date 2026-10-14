"""Dealer Portal accounts, managed from the website (Founder, 13 Oct 2026):
each vendor whose stock goes to the Dealer Portal has the dealer's own login.
Saved once here, then every stock file that vendor sends is pushed on its own
-- reset-own-stock, then the full list -- exactly like the Google Sheet tab.

Logins are written to the accounts file (DEALER_PORTAL_ACCOUNTS_FILE), never
the database, and a password is never sent back by any endpoint.

    GET    /api/dealer-portal/accounts               every account + last upload
    POST   /api/dealer-portal/accounts               {vendor_id, username, password, dealer_id?}
                                                     login is TESTED first; saved only if it works
    POST   /api/dealer-portal/accounts/{key}/preview what the next upload would send (nothing sent)
    POST   /api/dealer-portal/accounts/{key}/push    reset + upload now
    POST   /api/dealer-portal/accounts/{key}/enabled {enabled}
    DELETE /api/dealer-portal/accounts/{key}
"""

from __future__ import annotations

import base64
import json
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.integrations.dealer_portal import client, credentials, csv_builder, delta, mapping
from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from backend.app.integrations.dealer_portal.credentials import DealerPortalAccount
from core.logging_setup import get_logger
from core.models import DealerPortalPush, InventoryImport, Vendor

logger = get_logger(__name__)

router = APIRouter(prefix="/api/dealer-portal", tags=["dealer-portal"], dependencies=[Depends(get_current_user)])


class AccountIn(BaseModel):
    vendor_id: int
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    dealer_id: int | None = None


class EnabledIn(BaseModel):
    enabled: bool


def _key_for(vendor: Vendor) -> str:
    code = (vendor.vendor_code or "").upper().replace("CT_", "")
    base = code or vendor.name
    return re.sub(r"[^A-Z0-9]", "", base.upper())[:20] or f"V{vendor.id}"


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


def _check_login(account: DealerPortalAccount) -> dict:
    """Log in with these credentials, read who DP says they are, log out.
    Raises HTTPException with a plain message when the login fails."""
    try:
        token = client._login(account)
    except client.DealerPortalError as exc:
        message = str(exc)
        if "HTTP 401" in message or "HTTP 400" in message:
            message = "Wrong username or password (the portal refused the login)."
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=message) from None
    claims = _jwt_claims(token)
    try:
        client._requests().post(
            f"{dealer_portal_settings.base_url}/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
            timeout=dealer_portal_settings.timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "dealer_id": claims.get("dealer_id"),
        "display_name": claims.get("display_name"),
        "can_upload": "stock.upload" in (claims.get("permissions") or []),
    }


def _account(key: str, db: Session) -> DealerPortalAccount:
    account = credentials.account_by_key(key, db)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No Dealer Portal account {key}.")
    return account


def _out(account: DealerPortalAccount, db: Session) -> dict:
    credentials.resolve_account_vendors(account, db)
    last = db.execute(
        select(DealerPortalPush).where(DealerPortalPush.account_key == account.key).order_by(DealerPortalPush.id.desc()).limit(1)
    ).scalar_one_or_none()
    stock = []
    for vid in account.vendor_ids:
        imp = db.execute(
            select(InventoryImport).where(InventoryImport.vendor_id == vid, InventoryImport.is_active.is_(True))
        ).scalar_one_or_none()
        if imp is not None:
            stock.append({"vendor_id": vid, "rows": imp.row_count, "file": imp.file_name, "at": imp.created_at})
    dealer_id = account.dealer_id
    if dealer_id is None and account.vendor_ids:
        row = mapping.get_map(account.vendor_ids[0], db)
        dealer_id = row.dp_dealer_id if row is not None else None
    return {
        "key": account.key,
        "source": account.source,
        "vendors": [{"id": i, "name": n} for i, n in zip(account.vendor_ids, account.vendor_names)],
        "username": account.username,
        "dealer_id": dealer_id,
        "enabled": account.enabled,
        "full_snapshot": account.full_snapshot,
        "reset_before_upload": account.reset_before_upload,
        "stock": stock,
        "last_push": None if last is None else {
            "at": last.created_at,
            "status": last.status.value if hasattr(last.status, "value") else str(last.status),
            "rows_sent": last.rows_sent,
            "inserted": last.inserted_count,
            "updated": last.updated_count,
            "failed": last.failed_count,
            "error": getattr(last, "error", None),
        },
    }


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)) -> dict:
    accounts = [_out(a, db) for a in credentials.load_accounts()]
    return {
        "accounts": accounts,
        "file_configured": bool(dealer_portal_settings.accounts_file),
        "push_enabled": dealer_portal_settings.enabled,
    }


@router.post("/accounts")
def save_account(body: AccountIn, db: Session = Depends(get_db)) -> dict:
    vendor = db.get(Vendor, body.vendor_id)
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Vendor not found.")
    if not dealer_portal_settings.accounts_file:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="DEALER_PORTAL_ACCOUNTS_FILE is not set on the server.")
    # Already connected (server settings or an earlier save)? Keep its key, so
    # the new login REPLACES the old one -- e.g. after a password change.
    key = _key_for(vendor)
    vendor_patterns = [vendor.name]
    for existing in credentials.load_accounts():
        credentials.resolve_account_vendors(existing, db)
        if vendor.id in existing.vendor_ids:
            key = existing.key
            vendor_patterns = existing.vendor_patterns or [vendor.name]
            break
    account = DealerPortalAccount(
        key=key, username=body.username.strip(), password=body.password, token=None,
        device_id=f"procurehub-{key.lower()}", vendor_patterns=vendor_patterns,
        full_snapshot=True, tat_days=dealer_portal_settings.tat_days, enabled=True,
        reset_before_upload=True, source="file",
    )
    who = _check_login(account)
    if who["dealer_id"] is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Login worked but the portal did not say which dealer it is.")
    if body.dealer_id is not None and int(who["dealer_id"]) != int(body.dealer_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"This login belongs to dealer {who['dealer_id']} ({who['display_name']}), not {body.dealer_id}. Nothing saved.",
        )
    if not who["can_upload"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="This dealer account has no stock-upload permission on the portal.")
    credentials.save_file_account({
        "key": key,
        "username": account.username,
        "password": body.password,
        "vendors": vendor_patterns,
        "dealer_id": int(who["dealer_id"]),
        "dealer_name": who["display_name"],
        "full_snapshot": True,
        "reset_before_upload": True,
        "enabled": True,
    })
    mapping.confirm_from_login(vendor.id, vendor.name, int(who["dealer_id"]), who["display_name"], db)
    db.commit()
    logger.info("Dealer Portal account %s saved for vendor %s -> dealer %s.", key, vendor.name, who["dealer_id"])
    saved = credentials.account_by_key(key, db)
    return {"saved": True, "login": who, "account": _out(saved, db) if saved else None}


@router.post("/accounts/{key}/preview")
def preview(key: str, db: Session = Depends(get_db)) -> dict:
    account = _account(key, db)
    result = delta.build_delta(account, db)
    return {
        "key": account.key,
        "rows_to_send": result.rows_sent,
        "zeroed": result.zeroed_count,
        "no_price": result.priceless_count,
        "duplicates_collapsed": result.duplicate_count,
        "reset_first": account.full_snapshot and account.reset_before_upload,
        "sample": csv_builder.preview(result.rows, tat_days=account.tat_days, limit=8),
    }


@router.post("/accounts/{key}/push")
def push_now(key: str, db: Session = Depends(get_db)) -> dict:
    from backend.app.integrations.dealer_portal import push_service

    account = _account(key, db)
    with push_service._lock_for(account.key):
        result = push_service.push_account(account)
    db.expire_all()
    return {"status": getattr(result, "value", str(result)), "account": _out(_account(key, db), db)}


@router.post("/accounts/{key}/enabled")
def set_enabled(key: str, body: EnabledIn, db: Session = Depends(get_db)) -> dict:
    account = _account(key, db)
    if account.source != "file":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This account is in the server settings; change it there.")
    credentials.update_file_account(account.key, enabled=body.enabled)
    return _out(_account(key, db), db)


@router.delete("/accounts/{key}")
def remove(key: str, db: Session = Depends(get_db)) -> dict:
    account = _account(key, db)
    if account.source != "file":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This account is in the server settings; remove it there.")
    credentials.remove_file_account(account.key)
    return {"removed": account.key}
