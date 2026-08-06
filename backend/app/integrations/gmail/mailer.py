"""Outbound email via the same Gmail account already configured for reading
Customer Orders (`backend/app/integrations/gmail/config.py`) -- used by the
Automatic Vendor Allocation Report email and internal Purchase Order email,
not by the Gmail Customer Order polling path itself.

Dual-mode, same as the rest of this integration:
- `imap` mode: SMTP with the same App Password already configured for IMAP
  (Gmail accepts SMTP AUTH with an App Password on smtp.gmail.com:465).
- `oauth` mode: the Gmail API's `users().messages().send()`, via the same
  OAuth refresh-token credentials already used for reading.

`send_email` never raises -- any failure is logged and returns False, so a
mail outage never breaks the caller's larger workflow (auto-select, PO
generation). This mirrors the "integration failures are logged, never
fatal" convention used throughout this app (see e.g.
`backend/app/integrations/google_sheets/sync_service.py`).
"""

from __future__ import annotations

import base64
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from backend.app.integrations.gmail.config import GmailSettings, gmail_settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

XLSX_MIME_SUBTYPE = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _build_message(
    settings: GmailSettings,
    to: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes]],
) -> MIMEMultipart:
    message = MIMEMultipart()
    message["From"] = settings.email or ""
    message["To"] = to
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    for filename, content in attachments:
        part = MIMEApplication(content, _subtype=XLSX_MIME_SUBTYPE)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        message.attach(part)

    return message


def _send_via_smtp(settings: GmailSettings, to: str, message: MIMEMultipart) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(settings.email, settings.app_password)
        smtp.sendmail(settings.email, [to], message.as_string())


def _send_via_gmail_api(settings: GmailSettings, message: MIMEMultipart) -> None:
    # Imported lazily so an IMAP-only deployment never needs the Google API
    # client libraries importable.
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials(
        None,
        refresh_token=settings.refresh_token,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_email(
    to: str, subject: str, body: str, attachments: list[tuple[str, bytes]] | None = None
) -> bool:
    """Returns True on success, False on any failure (logged, never raised)."""
    if not gmail_settings.is_configured():
        logger.warning(
            "Cannot send email %r -- Gmail is not configured (GMAIL_AUTH_MODE=%s).",
            subject,
            gmail_settings.auth_mode,
        )
        return False

    message = _build_message(gmail_settings, to, subject, body, attachments or [])

    try:
        if gmail_settings.auth_mode == "oauth":
            _send_via_gmail_api(gmail_settings, message)
        else:
            _send_via_smtp(gmail_settings, to, message)
        return True
    except Exception:  # noqa: BLE001 -- a mail failure must never crash the caller
        logger.exception("Failed to send email %r to %s", subject, to)
        return False
