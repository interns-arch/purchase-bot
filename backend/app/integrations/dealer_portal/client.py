"""HTTP client for the Dealer Portal stock upload API.

    POST /auth/login                 -> access_token
    POST /stock/upload-stock-csv     -> batch_id + per-row counts
    POST /auth/logout

TOKEN HANDLING -- BOTH MECHANISMS, CHOSEN BY ENV
------------------------------------------------
The verified integration pack documents an INTERACTIVE login: an 8-hour JWT
tied to a `device_id`, where a second active device id produces
`409 SESSION_ALREADY_ACTIVE`. The guide itself flags that as a limitation to
fix (known limitation #4), and the direction is to move to a permanent /
refresh token.

Both are supported here, and which one runs is decided purely by env:

  * `DEALER_PORTAL_<KEY>_TOKEN` set  -> that token is used as the bearer
    token directly. NO login call, no logout, no device id, no 8-hour
    expiry, no 409 collision. Switch to this the moment Vineet issues the
    long-lived credentials -- nothing else in ProcureHub changes.

  * otherwise -> username/password login, token cached in memory for the
    process and refreshed shortly before it expires, plus a single retry on
    a 401 in case the server expired it early.

Nothing here raises to the caller's import: `push_service` wraps every call
and treats any failure as a FAILED audit row.

DEALER IDENTITY: never send `dealer_id`. DP takes the dealer from the token
itself, and a `dealer_id` query parameter is ignored.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from backend.app.integrations.dealer_portal.credentials import DealerPortalAccount
from core.logging_setup import get_logger

logger = get_logger(__name__)


class DealerPortalError(RuntimeError):
    """Any failure talking to Dealer Portal. Always caught by push_service."""


@dataclass
class UploadResult:
    batch_id: str | None
    total_rows: int
    inserted_count: int
    updated_count: int
    failed_count: int
    status: str | None = None


# Cached login tokens, keyed by account key: {key: (token, expires_at_epoch)}
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def _requests():
    """Imported lazily so that merely importing this integration never adds a
    hard dependency to a ProcureHub process that will not use it."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise DealerPortalError(
            "The 'requests' package is required for the Dealer Portal integration."
        ) from exc
    return requests


def _login(account: DealerPortalAccount) -> str:
    requests = _requests()
    url = f"{dealer_portal_settings.base_url}/auth/login"
    payload = {
        "username": account.username,
        "password": account.password,
        "device_id": account.device_id,
        "device_info": "ProcureHub stock push",
    }
    try:
        response = requests.post(
            url, json=payload, timeout=dealer_portal_settings.timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 -- network layer
        raise DealerPortalError(f"Dealer Portal login request failed: {exc}") from exc

    if response.status_code == 409:
        raise DealerPortalError(
            "Dealer Portal login returned 409 SESSION_ALREADY_ACTIVE for account "
            f"{account.key}. Another session holds this dealer's device id. Set "
            f"DEALER_PORTAL_{account.key}_DEVICE_ID to the id already in use, or "
            f"move this account to DEALER_PORTAL_{account.key}_TOKEN."
        )
    if response.status_code != 200:
        raise DealerPortalError(
            f"Dealer Portal login failed for {account.key}: HTTP {response.status_code}."
        )

    try:
        token = (response.json() or {}).get("access_token")
    except ValueError as exc:
        raise DealerPortalError(
            f"Dealer Portal login for {account.key} returned a non-JSON body."
        ) from exc

    if not token:
        raise DealerPortalError(
            f"Dealer Portal login for {account.key} returned no access_token."
        )
    return token


def _cached_token(account: DealerPortalAccount, *, force_refresh: bool = False) -> str:
    """A usable bearer token for this account."""
    if account.uses_permanent_token:
        return account.token or ""

    with _token_lock:
        if not force_refresh:
            cached = _token_cache.get(account.key)
            if cached and cached[1] > time.time():
                return cached[0]

        token = _login(account)
        # The guide documents ~28800s (8h); refresh a little early.
        expires_at = (
            time.time() + 28800 - dealer_portal_settings.token_refresh_margin_seconds
        )
        _token_cache[account.key] = (token, expires_at)
        logger.info("Dealer Portal: obtained a new access token for %s.", account.key)
        return token


def _parse_upload_response(response) -> UploadResult:
    try:
        body = response.json() or {}
    except ValueError:
        body = {}
    return UploadResult(
        batch_id=body.get("batch_id"),
        total_rows=int(body.get("total_rows") or 0),
        inserted_count=int(body.get("inserted_count") or 0),
        updated_count=int(body.get("updated_count") or 0),
        failed_count=int(body.get("failed_count") or 0),
        status=body.get("status"),
    )


def upload_csv(
    account: DealerPortalAccount, csv_bytes: bytes, *, file_name: str = "stock.csv"
) -> UploadResult:
    """POST the CSV to `/stock/upload-stock-csv` for this account.

    The multipart field name must be exactly `file`. Raises
    `DealerPortalError` on any failure -- the caller records that as a FAILED
    push and retries later. Because DP's semantics are absolute-replace, a
    retry of the identical CSV is harmless: it cannot double-count."""
    if not csv_bytes:
        raise DealerPortalError("Refusing to upload an empty CSV.")

    requests = _requests()
    url = f"{dealer_portal_settings.base_url}/stock/upload-stock-csv"

    def _post(token: str):
        return requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (file_name, csv_bytes, "text/csv")},
            timeout=dealer_portal_settings.timeout_seconds,
        )

    token = _cached_token(account)
    try:
        response = _post(token)
        # A login token can be expired or logged out server-side before our
        # cached expiry; one forced refresh, then give up.
        if response.status_code == 401 and not account.uses_permanent_token:
            logger.info(
                "Dealer Portal: token for %s rejected (401) -- logging in again.",
                account.key,
            )
            response = _post(_cached_token(account, force_refresh=True))
    except DealerPortalError:
        raise
    except Exception as exc:  # noqa: BLE001 -- network layer
        raise DealerPortalError(f"Dealer Portal upload request failed: {exc}") from exc

    if response.status_code == 403:
        raise DealerPortalError(
            f"Dealer Portal upload for {account.key} returned 403: this account "
            "lacks the 'stock.upload' permission. A shared vendor_api_client "
            "account cannot call this route -- a real dealer account is required."
        )
    if response.status_code != 200:
        raise DealerPortalError(
            f"Dealer Portal upload for {account.key} failed: HTTP "
            f"{response.status_code}."
        )

    return _parse_upload_response(response)


def logout(account: DealerPortalAccount) -> None:
    """Best-effort logout of a login-issued token. Never raises, and does
    nothing for a permanent token (logging that out would break the next
    push)."""
    if account.uses_permanent_token:
        return
    with _token_lock:
        cached = _token_cache.pop(account.key, None)
    if not cached:
        return
    try:
        requests = _requests()
        requests.post(
            f"{dealer_portal_settings.base_url}/auth/logout",
            headers={"Authorization": f"Bearer {cached[0]}"},
            timeout=dealer_portal_settings.timeout_seconds,
        )
    except Exception:  # noqa: BLE001 -- an output must never affect the import
        logger.debug("Dealer Portal logout for %s did not complete.", account.key)
