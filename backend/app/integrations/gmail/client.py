"""Gmail client -- two backends behind one interface, selected by
`GmailSettings.auth_mode`. Both produce the same `IncomingEmailMessage`
shape so `backend/app/workers/email_worker.py` never needs to know which
mode is active.

- `ImapGmailClient`: stdlib `imaplib`/`email` only, App Password auth.
  Fetches with `BODY.PEEK[]` (not plain `RFC822`, which implicitly marks a
  message `\\Seen` on fetch) so a message is only marked read after this
  worker has actually finished with it.
- `OAuthGmailClient`: Gmail API via a long-lived refresh token.

`provider_ref` on `IncomingEmailMessage` is an opaque handle each backend
uses internally to mark a message processed later (an IMAP UID, or the
Gmail API message id) -- the worker never inspects it.
"""

from __future__ import annotations

import base64
import email
import imaplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr

from core.logging_setup import get_logger

from backend.app.integrations.gmail.config import GmailSettings

logger = get_logger(__name__)


class GmailNotConfiguredError(Exception):
    """Raised when the selected `auth_mode`'s required credentials are
    missing -- the caller logs this and skips the poll, never crashes."""


@dataclass
class EmailAttachment:
    filename: str
    content: bytes


@dataclass
class IncomingEmailMessage:
    message_id: str
    sender: str | None
    subject: str | None
    provider_ref: str
    attachments: list[EmailAttachment] = field(default_factory=list)


class GmailClient(ABC):
    @abstractmethod
    def fetch_unread_messages(self) -> list[IncomingEmailMessage]:
        raise NotImplementedError

    @abstractmethod
    def mark_processed(self, message: IncomingEmailMessage) -> None:
        raise NotImplementedError


def ist_midnight_epoch() -> int:
    """Epoch seconds of TODAY 00:00 in the business timezone (IST). Gmail's
    `after:` operator accepts an epoch timestamp, making the boundary exact
    regardless of the Google account's own timezone setting."""
    from core.time_utils import now_ist

    midnight = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def build_search_query(settings: GmailSettings) -> str:
    """The Gmail search used by the OAuth client. With GMAIL_ALLOWED_SENDERS
    set, the query itself is narrowed with a `from:(a OR b)` clause so
    non-whitelisted mail is never even fetched from the API. With
    GMAIL_PROCESS_TODAY_ONLY (default), only mail received TODAY (IST) is
    fetched -- an unread mail from a previous day stays unread and untouched."""
    query = "is:unread has:attachment"
    if settings.allowed_senders:
        senders = " OR ".join(settings.allowed_senders)
        query += f" from:({senders})"
    if settings.today_only:
        query += f" after:{ist_midnight_epoch()}"
    return query


def sender_allowed(sender: str | None, settings: GmailSettings) -> bool:
    """True when this From header may be processed. An empty whitelist keeps
    the historical accept-all behaviour. Matching uses only the address part
    ("Rahul Traders <rahul@acme.com>" -> "rahul@acme.com"), case-insensitive."""
    if not settings.allowed_senders:
        return True
    if not sender:
        return False
    address = parseaddr(sender)[1].strip().lower()
    return address in settings.allowed_senders


def _decode_mime_words(value: str | None) -> str | None:
    if not value:
        return value
    decoded_parts = decode_header(value)
    return "".join(
        part.decode(encoding or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, encoding in decoded_parts
    )


def _extract_attachments(parsed_email: Message) -> list[EmailAttachment]:
    attachments: list[EmailAttachment] = []
    for part in parsed_email.walk():
        content_disposition = str(part.get("Content-Disposition") or "")
        if "attachment" not in content_disposition.lower():
            continue
        filename = _decode_mime_words(part.get_filename())
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachments.append(EmailAttachment(filename=filename, content=payload))
    return attachments


class ImapGmailClient(GmailClient):
    def __init__(self, settings: GmailSettings):
        self._settings = settings

    def _connect(self) -> imaplib.IMAP4_SSL:
        connection = imaplib.IMAP4_SSL(self._settings.imap_server, self._settings.imap_port)
        connection.login(self._settings.email, self._settings.app_password)
        connection.select("INBOX")
        return connection

    def fetch_unread_messages(self) -> list[IncomingEmailMessage]:
        connection = self._connect()
        try:
            status, data = connection.uid("search", None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return []

            messages: list[IncomingEmailMessage] = []
            for uid in data[0].split():
                status, msg_data = connection.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                parsed = email.message_from_bytes(raw_email)
                message_id = parsed.get("Message-ID") or f"imap-uid-{uid.decode()}"
                sender = _decode_mime_words(parsed.get("From"))
                if not sender_allowed(sender, self._settings):
                    logger.info(
                        "Gmail (IMAP): skipping message from %r -- sender is not "
                        "in GMAIL_ALLOWED_SENDERS.",
                        sender,
                    )
                    continue
                messages.append(
                    IncomingEmailMessage(
                        message_id=message_id,
                        sender=sender,
                        subject=_decode_mime_words(parsed.get("Subject")),
                        provider_ref=uid.decode(),
                        attachments=_extract_attachments(parsed),
                    )
                )
            return messages
        finally:
            connection.logout()

    def mark_processed(self, message: IncomingEmailMessage) -> None:
        connection = self._connect()
        try:
            connection.uid("store", message.provider_ref, "+FLAGS", "(\\Seen)")
        finally:
            connection.logout()


class OAuthGmailClient(GmailClient):
    def __init__(self, settings: GmailSettings):
        self._settings = settings

    def _build_service(self):
        # Imported lazily so an IMAP-only deployment never needs the Google
        # API client libraries installed.
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials(
            None,
            refresh_token=self._settings.refresh_token,
            client_id=self._settings.client_id,
            client_secret=self._settings.client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def _extract_attachments(self, service, message_id: str, payload: dict) -> list[EmailAttachment]:
        attachments: list[EmailAttachment] = []
        for part in payload.get("parts") or []:
            filename = part.get("filename")
            body = part.get("body") or {}
            attachment_id = body.get("attachmentId")
            if filename and attachment_id:
                attachment = (
                    service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=attachment_id)
                    .execute()
                )
                data = base64.urlsafe_b64decode(attachment["data"])
                attachments.append(EmailAttachment(filename=filename, content=data))
            elif part.get("parts"):
                attachments.extend(self._extract_attachments(service, message_id, part))
        return attachments

    def fetch_unread_messages(self) -> list[IncomingEmailMessage]:
        service = self._build_service()
        response = (
            service.users()
            .messages()
            .list(userId="me", q=build_search_query(self._settings), maxResults=25)
            .execute()
        )
        messages: list[IncomingEmailMessage] = []
        for ref in response.get("messages", []):
            full = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            payload = full.get("payload", {})
            headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
            sender = headers.get("From")
            # Defense in depth: even if the Gmail query matched more broadly
            # than intended, a sender outside the whitelist is never processed
            # (and never marked read -- the message stays untouched).
            if not sender_allowed(sender, self._settings):
                logger.info(
                    "Gmail: skipping message from %r -- sender is not in "
                    "GMAIL_ALLOWED_SENDERS.",
                    sender,
                )
                continue
            # Same defense for the received-today rule: `internalDate` is
            # Gmail's authoritative receive time in epoch milliseconds.
            if self._settings.today_only:
                received_ms = int(full.get("internalDate") or 0)
                if received_ms and received_ms < ist_midnight_epoch() * 1000:
                    logger.info(
                        "Gmail: skipping message from %r -- received before "
                        "today (IST); it stays unread and untouched.",
                        sender,
                    )
                    continue
            messages.append(
                IncomingEmailMessage(
                    message_id=headers.get("Message-ID") or ref["id"],
                    sender=sender,
                    subject=headers.get("Subject"),
                    provider_ref=ref["id"],
                    attachments=self._extract_attachments(service, ref["id"], payload),
                )
            )
        return messages

    def mark_processed(self, message: IncomingEmailMessage) -> None:
        service = self._build_service()
        service.users().messages().modify(
            userId="me", id=message.provider_ref, body={"removeLabelIds": ["UNREAD"]}
        ).execute()


def get_gmail_client(settings: GmailSettings) -> GmailClient:
    if not settings.is_configured():
        raise GmailNotConfiguredError(
            f"Gmail auth_mode={settings.auth_mode!r} is missing required credentials "
            "(see .env.example) -- skipping this poll."
        )
    if settings.auth_mode == "oauth":
        return OAuthGmailClient(settings)
    return ImapGmailClient(settings)
