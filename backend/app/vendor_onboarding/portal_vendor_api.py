"""Create an approved vendor on the Dealer Portal DIRECTLY (12 Nov 2026).

Until now an approved vendor went out as a "[Data Entry] Vendor Creation
Request" mail and the sales bot's data-entry robot made the Portal account.
That robot was switched off on 7 Nov, so OM SAI RAM ENTERPRISES (VR-19) was
approved and never reached the Portal. The Portal team has now given the
admin login + `POST /users/vendor/create`, so ProcureHub calls it itself:

    1. POST /auth/login            admin login (DEALER_PORTAL_ADMIN_USERNAME/_PASSWORD)
    2. duplicate check             is this MOBILE or GSTIN already on the Portal?
                                   yes -> no new account, the existing one is reported
    3. POST /users/vendor/create   vendor_name, phone, username, password, email,
                                   gst_no, address, credit_days, credit_limit,
                                   also_add_as_customer (the form's Haan/Nahi)
    4. POST /auth/logout

Every call carries the Cloudflare Access service token. The admin password
lives in backend/.env only and is never logged.

Duplicate check: the Portal has no "is this GSTIN taken" route, so two lists
are read with the admin token -- `GET /users/` (every login, with its phone)
and `GET /dealers/` (every dealer/vendor account, with its GSTIN and phone).
Only the matching fields are looked at; nothing from either list is stored.
When NEITHER list can be read the vendor is NOT created (a duplicate account
is worse than a retry) and the request goes to CREATE_FAILED for a retry.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import httpx

from backend.app.integrations.dealer_portal.config import dealer_portal_settings
from core.logging_setup import get_logger

logger = get_logger(__name__)


class _Settings:
    username: str = (os.environ.get("DEALER_PORTAL_ADMIN_USERNAME") or "").strip()
    password: str = (os.environ.get("DEALER_PORTAL_ADMIN_PASSWORD") or "").strip()
    # Its own device id, so this login never collides with the admin's own
    # browser session or another bot's (409 SESSION_ALREADY_ACTIVE).
    device_id: str = (os.environ.get("DEALER_PORTAL_ADMIN_DEVICE_ID") or "procurehub-vendor-create").strip()
    timeout: float = float(os.environ.get("DEALER_PORTAL_VENDOR_API_TIMEOUT", "30") or 30)
    # The two roster reads are big (8,000+ rows); give them longer.
    list_timeout: float = float(os.environ.get("DEALER_PORTAL_LIST_TIMEOUT", "120") or 120)

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)


settings = _Settings()


class PortalApiError(RuntimeError):
    pass


@dataclass
class Existing:
    account_id: str | None
    name: str
    matched_on: str  # "mobile 9xxxxxxxxx" / "GSTIN 07..."


@dataclass
class Created:
    ok: bool
    account_id: str | None = None
    username: str | None = None
    password: str | None = None
    existing: Existing | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _url(path: str) -> str:
    return f"{dealer_portal_settings.base_url}{path}"


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", **dealer_portal_settings.access_headers()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _detail(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error")
        if isinstance(detail, (list, dict)):
            return str(detail)[:300]
        return str(detail or body)[:300]
    return str(body)[:300]


def login() -> str:
    try:
        response = httpx.post(
            _url("/auth/login"),
            json={
                "username": settings.username,
                "password": settings.password,
                "device_id": settings.device_id,
                "device_info": "ProcureHub vendor creation",
            },
            headers=_headers(),
            timeout=settings.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise PortalApiError(f"Dealer Portal unreachable: {exc.__class__.__name__}") from exc
    if "cloudflare" in (response.text or "")[:2000].lower() and response.status_code in (302, 403):
        raise PortalApiError("Dealer Portal: Cloudflare Access refused the service token (DEALER_PORTAL_CF_CLIENT_ID/_SECRET).")
    if response.status_code != 200:
        raise PortalApiError(f"Dealer Portal admin login failed: HTTP {response.status_code} {_detail(response)}")
    token = (response.json() or {}).get("access_token")
    if not token:
        raise PortalApiError("Dealer Portal admin login returned no access_token.")
    return token


def logout(token: str) -> None:
    try:
        httpx.post(_url("/auth/logout"), headers=_headers(token), timeout=settings.timeout)
    except Exception:  # noqa: BLE001 -- best effort
        logger.debug("Dealer Portal admin logout failed.", exc_info=True)


def _get_rows(token: str, path: str) -> list[dict]:
    response = httpx.get(_url(path), headers=_headers(token), timeout=settings.list_timeout, follow_redirects=True)
    if response.status_code != 200:
        raise PortalApiError(f"GET {path}: HTTP {response.status_code}")
    data = response.json()
    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or data.get("users") or data.get("results") or []
    return [row for row in data or [] if isinstance(row, dict)]


# --------------------------------------------------------------------------
# Duplicate check
# --------------------------------------------------------------------------

_GST_KEYS = ("gst_no", "gstin", "gst_number", "gst")
_PHONE_KEYS = ("phone", "mobile", "phone_no", "phone_number", "contact_no", "mobile_no")
_NAME_KEYS = ("vendor_name", "dealer_name", "name", "username")
_ID_KEYS = ("dealer_id", "vendor_id", "account_id", "user_id", "id")


def _digits10(value) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


def _first(row: dict, keys) -> str:
    for key in keys:
        if row.get(key) not in (None, ""):
            return str(row[key]).strip()
    return ""


def find_existing(token: str, *, gstin: str | None, mobile: str | None) -> tuple[Existing | None, set[str], list[str]]:
    """(existing account or None, every Portal username, notes). Raises
    PortalApiError when neither roster could be read -- nothing was checked."""
    gstin = (gstin or "").strip().upper()
    mobile = _digits10(mobile)
    usernames: set[str] = set()
    notes: list[str] = []
    read_any = False
    gst_checked = False

    for path in ("/dealers/", "/users/?limit=10000"):
        try:
            rows = _get_rows(token, path)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{path} not read ({exc.__class__.__name__}: {str(exc)[:80]})")
            logger.warning("Vendor duplicate check: %s not read: %s", path, exc)
            continue
        read_any = True
        for row in rows:
            if row.get("username"):
                usernames.add(str(row["username"]).strip().lower())
            row_gst = _first(row, _GST_KEYS).upper()
            if row_gst and row_gst != "NULL":
                gst_checked = True
            hit = None
            if gstin and row_gst == gstin:
                hit = f"GSTIN {gstin}"
            elif mobile and len(mobile) == 10 and _digits10(_first(row, _PHONE_KEYS)) == mobile:
                hit = f"mobile {mobile}"
            if hit:
                return Existing(_first(row, _ID_KEYS) or None, _first(row, _NAME_KEYS) or "?", hit), usernames, notes
    if not read_any:
        raise PortalApiError("could not check the Portal for an existing vendor: " + "; ".join(notes))
    if gstin and not gst_checked:
        notes.append("the Portal lists carry no GSTIN, so only the mobile was checked")
    return None, usernames, notes


# --------------------------------------------------------------------------
# Username / password -- the data-entry robot's rules, so accounts look alike
# --------------------------------------------------------------------------


def _word(value) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _firm(name: str) -> str:
    return re.sub(r"^\s*m\s*[/.]\s*s\.?\s*", "", name or "", flags=re.IGNORECASE).strip()


def username_candidates(answers: dict, gst: dict | None) -> list[str]:
    base = _word(_firm(answers.get("vendor_name") or ""))[:50]
    if not base:
        return []
    address = answers.get("address") or (gst or {}).get("address") or ""
    places = [_word(p) for p in str(address).split(",") if p.strip() and not re.search(r"\d", p)]
    contact = _word(str(answers.get("contact_person") or "").split(" ")[0])
    out = [base]
    for extra in [*(reversed(places)), contact, "vendor"]:
        if extra and extra not in base:
            candidate = f"{base}_{extra}"[:60].strip("_")
            if candidate not in out:
                out.append(candidate)
    return out


def password_for(answers: dict) -> str:
    """Contact person's first name in small letters + @123 (founder, 12 Sep),
    else the firm's first word."""
    contact = str(answers.get("contact_person") or "").strip()
    source = contact if re.search(r"[A-Za-z]", contact) else _firm(answers.get("vendor_name") or "")
    first = re.sub(r"[^a-z0-9]", "", (source.split() or ["vendor"])[0].lower()) or "vendor"
    return f"{first}@123"


def _number(value, cast):
    try:
        return cast(str(value).replace(",", "").strip()) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def also_customer(answers: dict) -> bool:
    return str(answers.get("also_customer") or "").strip().lower() == "haan"


def vendor_body(answers: dict, gst: dict | None, username: str, password: str) -> dict:
    address = answers.get("address") or (gst or {}).get("address") or ""
    pincode = (gst or {}).get("pincode")
    if address and pincode and str(pincode) not in address:
        address = f"{address} - {pincode}"
    return {
        "vendor_name": answers.get("vendor_name"),
        "phone": _digits10(answers.get("mobile")),
        "username": username,
        "password": password,
        "email": answers.get("email") or "",
        "gst_no": (answers.get("gstin") or "").upper(),
        "address": address,
        "credit_days": _number(answers.get("credit_days"), int),
        "credit_limit": _number(answers.get("credit_limit"), float),
        "also_add_as_customer": also_customer(answers),
    }


# --------------------------------------------------------------------------
# The whole step
# --------------------------------------------------------------------------


def create_vendor(answers: dict, *, gst: dict | None) -> Created:
    if not settings.enabled:
        return Created(ok=False, error="DEALER_PORTAL_ADMIN_USERNAME / _PASSWORD are not set")
    try:
        token = login()
    except PortalApiError as exc:
        return Created(ok=False, error=str(exc))
    try:
        try:
            existing, usernames, notes = find_existing(token, gstin=answers.get("gstin"), mobile=answers.get("mobile"))
        except PortalApiError as exc:
            return Created(ok=False, error=str(exc))
        if existing is not None:
            logger.info("Vendor %s already on the Portal (%s, id %s) -- not created again.",
                        answers.get("vendor_name"), existing.matched_on, existing.account_id)
            return Created(ok=True, account_id=existing.account_id, existing=existing, notes=notes)

        candidates = [c for c in username_candidates(answers, gst) if c not in usernames]
        if not candidates:
            return Created(ok=False, error="no free Portal username for this vendor name", notes=notes)
        username, password = candidates[0], password_for(answers)
        body = vendor_body(answers, gst, username, password)
        try:
            response = httpx.post(_url("/users/vendor/create"), json=body, headers=_headers(token), timeout=settings.timeout)
        except Exception as exc:  # noqa: BLE001
            return Created(ok=False, error=f"Dealer Portal unreachable: {exc.__class__.__name__}", notes=notes)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            error = f"Dealer Portal refused the vendor: HTTP {response.status_code} {_detail(response)}"
            logger.warning("%s: %s", answers.get("vendor_name"), error)
            return Created(ok=False, error=error, notes=notes)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        account_id = None
        if isinstance(data, dict):
            account_id = str(_first(data, ("account_id", "vendor_id", "dealer_id", "user_id", "id")) or "") or None
        logger.info("Vendor %s created on the Portal: id %s, login %s, also customer %s.",
                    answers.get("vendor_name"), account_id, username, body["also_add_as_customer"])
        return Created(ok=True, account_id=account_id, username=username, password=password, notes=notes)
    finally:
        logout(token)
