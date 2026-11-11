"""Portal procurement settings, read from `backend/.env`. Same tiny-class
idiom as the other integrations.

The service account is a DEDICATED Dealer Portal login with only
purchase:view and purchase:create (brief). It is never a vendor's dealer
login and never a person's password. Secrets live in backend/.env (or the
file named by PORTAL_PROCUREMENT_SECRETS_FILE) and are never logged."""

from __future__ import annotations

import json
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _flag(name: str, default: str = "false") -> bool:
    return (os.environ.get(name) or default).strip().lower() == "true"


SOURCE = "dealer_portal"  # AdvanceOrder.source for Portal demand


class PortalProcurementSettings:
    # Master switch. Off: no poll, no vendor question, no Portal write.
    enabled: bool = _flag("PORTAL_PROCUREMENT_ENABLED")

    # The service account. Either here, or in a JSON file
    # {"username": ..., "password": ...} named by PORTAL_PROCUREMENT_SECRETS_FILE
    # (an AWS Secrets Manager export, a docker secret, ...).
    username: str = (os.environ.get("PORTAL_PROCUREMENT_USERNAME") or "").strip()
    password: str = (os.environ.get("PORTAL_PROCUREMENT_PASSWORD") or "").strip()
    secrets_file: str = (os.environ.get("PORTAL_PROCUREMENT_SECRETS_FILE") or "").strip()
    device_id: str = (os.environ.get("PORTAL_PROCUREMENT_DEVICE_ID") or "procurehub-vendor-bot-prod").strip()
    device_info: str = "ProcureHub AWS vendor bot"

    # How often the Active Procurement list is read, and how often the
    # allocation steps (allocate, knockoff, approval, Odoo, PDF) move on.
    poll_minutes: int = max(5, _int("PORTAL_PROCUREMENT_POLL_MINUTES", 30))
    step_minutes: int = max(1, _int("PORTAL_PROCUREMENT_STEP_MINUTES", 5))
    # Brief: the last seven calendar days (today minus six, IST).
    days: int = max(1, _int("PORTAL_PROCUREMENT_DAYS", 7))
    page_size: int = max(1, min(500, _int("PORTAL_PROCUREMENT_PAGE_SIZE", 500)))
    include_interbranch: bool = _flag("PORTAL_PROCUREMENT_INCLUDE_INTERBRANCH")

    # Vendor safety: at most this many NEW demand lines go to vendors per poll,
    # and at most this many parts in one vendor message. Lines over the cap
    # wait for the next poll -- vendors are never flooded (a bulk send got
    # Rajkumar's number banned, 2 Nov 2026).
    max_new_lines_per_poll: int = max(1, _int("PORTAL_PROCUREMENT_MAX_NEW_LINES", 25))
    max_lines_per_enquiry: int = max(1, _int("PORTAL_PROCUREMENT_LINES_PER_ENQUIRY", 10))

    # Who gets the PO PDF + summary. Blank = the admin numbers and purchase team.
    notify_numbers: list[str] = [
        x.strip() for x in (os.environ.get("PORTAL_PROCUREMENT_NOTIFY_NUMBERS") or "").split(",") if x.strip()
    ]
    # Operations alerts (stopped runs, 409s, onboarding needed). Blank = same as above.
    alert_numbers: list[str] = [
        x.strip() for x in (os.environ.get("PORTAL_PROCUREMENT_ALERT_NUMBERS") or "").split(",") if x.strip()
    ]

    timeout_seconds: float = float(os.environ.get("PORTAL_PROCUREMENT_TIMEOUT_SECONDS") or 60)
    get_attempts: int = max(1, _int("PORTAL_PROCUREMENT_GET_ATTEMPTS", 4))
    # A step that keeps failing goes to the dead-letter state after this many tries.
    max_step_attempts: int = max(1, _int("PORTAL_PROCUREMENT_MAX_STEP_ATTEMPTS", 5))
    # PDF sanity: anything smaller is not a real PO.
    min_pdf_bytes: int = max(100, _int("PORTAL_PROCUREMENT_MIN_PDF_BYTES", 1000))

    def credentials(self) -> tuple[str, str]:
        """(username, password); empty strings when not configured."""
        if self.secrets_file:
            try:
                with open(self.secrets_file, encoding="utf-8") as fh:
                    data = json.load(fh) or {}
                return str(data.get("username") or "").strip(), str(data.get("password") or "").strip()
            except (OSError, ValueError):
                return "", ""
        return self.username, self.password

    def configured(self) -> bool:
        user, password = self.credentials()
        return bool(user and password)


settings = PortalProcurementSettings()
