"""The Dealer Portal procurement API (brief, sections 1-10).

Retry rules, exactly as the brief sets them:
  * GET  -- 429 and transient 5xx / network errors are retried with
            exponential backoff + jitter, honouring Retry-After, bounded.
  * POST -- sent ONCE. A timeout or lost response raises `UnknownOutcome`;
            the caller must reconcile with a GET before trying again.
  * 400/401/403/404/409/422 -- never blind-retried. 401 refreshes the token
            once (refresh endpoint, else one fresh login); a second 401
            stops the run.

Every call carries the Cloudflare Access service headers AND the service
account JWT. Neither, nor the password, is ever logged or put in an error
message. TLS is verified (httpx default; never turned off)."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from backend.app.portal_procurement.config import settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PortalError(Exception):
    """A Portal call that failed. `status` is the HTTP status (None for a
    network failure); `detail` is the Portal's own message, sanitised."""

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None,
                 request_id: str | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.request_id = request_id
        self.body = body


class AuthStopped(PortalError):
    """Login refused, or a 401 survived one refresh. Stop the run, alert ops."""


class UnknownOutcome(PortalError):
    """A POST whose result never came back (timeout, connection drop, 502/504).
    It may or may not have taken effect -- reconcile with a GET, never re-POST
    blind."""


_SECRET_MARKERS = ("bearer ", "access_token", "refresh_token", "cf-access-client-secret", "password")


def _sanitise(text: Any, limit: int = 300) -> str | None:
    if text is None or text == "":
        return None
    text = str(text)
    if any(marker in text.lower() for marker in _SECRET_MARKERS):
        return "(response withheld: it contained credential-like text)"
    return text[:limit]


def _detail(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return _sanitise(response.text)
    if isinstance(body, dict):
        for key in ("detail", "message", "error", "msg"):
            value = body.get(key)
            if value:
                return _sanitise(value)
    return _sanitise(body)


def _request_id(response: httpx.Response) -> str | None:
    rid = response.headers.get("x-request-id") or response.headers.get("request-id")
    if rid:
        return rid[:80]
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("request_id"):
            return str(body["request_id"])[:80]
    except ValueError:
        pass
    return None


def _cloudflare_page(response: httpx.Response) -> bool:
    if "text/html" not in response.headers.get("content-type", ""):
        return False
    return "cloudflare" in (response.text or "")[:2000].lower()


@dataclass
class _Token:
    access: str
    refresh: str | None
    expires_at: float


class PortalClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, base_url: str | None = None):
        self._base = (base_url or dealer_portal_settings.base_url).rstrip("/")
        # verify=True is the httpx default and is never switched off.
        self._http = httpx.Client(timeout=settings.timeout_seconds, transport=transport)
        self._sleep = sleep
        self._token: _Token | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- auth
    def _base_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", **dealer_portal_settings.access_headers()}

    def _token_from(self, body: dict, old_refresh: str | None = None) -> _Token:
        expires_in = float(body.get("expires_in") or 28800)
        return _Token(body["access_token"], body.get("refresh_token") or old_refresh,
                      time.time() + max(60.0, expires_in - 300))

    def _login(self) -> _Token:
        user, password = settings.credentials()
        if not (user and password):
            raise AuthStopped("Portal procurement service account is not configured "
                              "(PORTAL_PROCUREMENT_USERNAME / _PASSWORD).")
        try:
            response = self._http.post(
                f"{self._base}/auth/login",
                json={"username": user, "password": password,
                      "device_id": settings.device_id, "device_info": settings.device_info},
                headers=self._base_headers(),
            )
        except httpx.HTTPError as exc:
            raise PortalError(f"Portal login unreachable: {exc.__class__.__name__}") from None
        if _cloudflare_page(response):
            raise AuthStopped("Cloudflare Access refused the service token (DEALER_PORTAL_CF_CLIENT_ID/_SECRET).",
                              status=response.status_code)
        if response.status_code != 200:
            # Invalid credentials are never retried in a loop.
            raise AuthStopped(f"Portal login refused: HTTP {response.status_code}",
                              status=response.status_code, detail=_detail(response))
        try:
            body = response.json() or {}
        except ValueError:
            raise AuthStopped("Portal login returned a non-JSON body.", status=200) from None
        if not body.get("access_token"):
            raise AuthStopped("Portal login returned no access_token.", status=200)
        logger.info("Portal procurement: logged in as the service account.")
        return self._token_from(body)

    def _refresh(self) -> _Token:
        """One refresh: the refresh endpoint when a refresh token was issued,
        else a single fresh login."""
        token = self._token
        if token and token.refresh:
            try:
                response = self._http.post(
                    f"{self._base}/auth/refresh-token",
                    json={"refresh_token": token.refresh},
                    headers=self._base_headers(),
                )
                if response.status_code == 200:
                    body = response.json() or {}
                    if body.get("access_token"):
                        return self._token_from(body, token.refresh)
            except (httpx.HTTPError, ValueError):
                pass
        return self._login()

    def _access(self, *, force: bool = False) -> str:
        with self._lock:
            if force and self._token is not None:
                self._token = self._refresh()
            elif self._token is None or self._token.expires_at <= time.time():
                self._token = self._login()
            return self._token.access

    def verify_token(self) -> bool:
        try:
            self.get("/auth/verify-token")
            return True
        except PortalError:
            return False

    # ------------------------------------------------------------- transport
    def _send(self, method: str, path: str, *, params=None, json=None, raw: bool = False):
        url = f"{self._base}{path}"
        is_get = method == "GET"
        attempts = settings.get_attempts if is_get else 1
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {**self._base_headers(), "Authorization": f"Bearer {self._access()}"}
            try:
                response = self._http.request(method, url, params=params, json=json, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if not is_get:
                    raise UnknownOutcome(f"{method} {path}: no response ({exc.__class__.__name__})") from None
                if attempt >= attempts:
                    raise PortalError(f"{method} {path}: unreachable ({exc.__class__.__name__})") from None
                self._backoff(attempt, None)
                continue

            if _cloudflare_page(response):
                raise AuthStopped(f"{method} {path}: Cloudflare Access refused the service token.",
                                  status=response.status_code)
            if response.status_code == 401:
                if refreshed:
                    raise AuthStopped(f"{method} {path}: 401 after a token refresh -- run stopped.", status=401)
                refreshed = True
                self._access(force=True)
                attempt -= 1  # the refresh retry is not a transient-error retry
                continue
            if is_get and response.status_code in RETRYABLE_STATUS and attempt < attempts:
                self._backoff(attempt, response.headers.get("retry-after"))
                continue
            if response.status_code >= 400:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                # A POST that hit a server error may have half-happened: unknown.
                lost = not is_get and response.status_code >= 500
                cls = UnknownOutcome if lost else PortalError
                raise cls(
                    f"{method} {path}: HTTP {response.status_code}",
                    status=response.status_code, detail=_detail(response),
                    request_id=_request_id(response), body=body,
                )
            if raw:
                return response
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                raise PortalError(f"{method} {path}: response was not JSON", status=response.status_code) from None

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = None
        if retry_after:
            try:
                delay = min(120.0, float(retry_after))
            except ValueError:
                delay = None
        if delay is None:
            delay = min(60.0, float(2 ** attempt)) + random.uniform(0, 1)
        self._sleep(delay)

    def get(self, path: str, params: dict | None = None, *, raw: bool = False):
        return self._send("GET", path, params=params, raw=raw)

    def post(self, path: str, payload: dict):
        return self._send("POST", path, json=payload)

    # ------------------------------------------------------------- endpoints
    def active(self, date_from: str, date_to: str, offset: int, limit: int, **filters) -> dict:
        params = {
            "date_from": date_from, "date_to": date_to,
            "include_po_mst": "false", "include_received": "false",
            "include_interbranch": "true" if settings.include_interbranch else "false",
            "limit": limit, "offset": offset, "sort_by": "order_date", "sort_dir": "asc",
        }
        params.update({k: v for k, v in filters.items() if v is not None})
        return self.get("/procurement/active", params)

    def active_with_po_mst(self, date_from: str, date_to: str, **filters) -> dict:
        """Used to reconcile an allocation whose POST outcome was lost."""
        params = {
            "date_from": date_from, "date_to": date_to, "include_po_mst": "true",
            "include_received": "false", "limit": 500, "offset": 0,
            "sort_by": "order_date", "sort_dir": "asc",
        }
        params.update({k: v for k, v in filters.items() if v is not None})
        return self.get("/procurement/active", params)

    def vendors_for_line(self, source_order_line_id: int) -> Any:
        return self.get(f"/procurement/active/{int(source_order_line_id)}/vendors")

    def allocate(self, payload: dict) -> dict:
        return self.post("/procurement/active/allocate", payload)

    def knockoff_preview(self, po_group_id: str, selected: list[int] | None = None) -> dict:
        return self.post("/procurement/knockoff/preview",
                         {"po_group_id": po_group_id, "selected_supply_order_line_ids": selected or []})

    def knockoff_commit(self, po_group_id: str, selected: list[int], reason: str) -> dict:
        return self.post("/procurement/knockoff/commit",
                         {"po_group_id": po_group_id, "selected_supply_order_line_ids": selected, "reason": reason})

    def request_duplicate_approval(self, po_group_id: str, reason: str) -> dict:
        return self.post("/procurement/knockoff/duplicate-approval", {"po_group_id": po_group_id, "reason": reason})

    def duplicate_approval_status(self, po_group_id: str) -> dict:
        return self.get(f"/procurement/knockoff/duplicate-approval/{po_group_id}")

    def push_to_odoo(self, po_group_id: str, vendor_dealer_id: int) -> dict:
        return self.post("/purchase-orders/push-to-odoo",
                         {"po_group_id": po_group_id, "vendor_dealer_id": int(vendor_dealer_id)})

    def purchase_order(self, po_group_id: str) -> dict:
        return self.get(f"/purchase-orders/{po_group_id}")

    def po_pdf(self, odoo_ref: str) -> httpx.Response:
        return self.get("/odoo/documents/po/pdf", {"odoo_ref": odoo_ref}, raw=True)

    def close(self) -> None:
        self._http.close()
