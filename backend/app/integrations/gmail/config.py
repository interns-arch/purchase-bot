"""Gmail Customer Order Automation configuration, read from environment
variables (`backend/.env`). Same tiny-class-with-`os.environ.get` idiom as
`backend/app/integrations/whatsapp/config.py`, kept in its own module for
the same reason: this integration should be able to evolve (or be disabled
entirely) without touching unrelated settings.

`GMAIL_AUTH_MODE` picks which client backend
(`backend/app/integrations/gmail/client.py`) is used -- `imap` (App
Password) or `oauth` (Gmail API refresh token). Switching modes is a
config-only change; no code changes are needed."""

from __future__ import annotations

import os


class GmailSettings:
    enabled: bool = (
        os.environ.get("GMAIL_ENABLED", os.environ.get("ENABLE_EMAIL_AUTOMATION", "false"))
        .strip()
        .lower()
        == "true"
    )
    auth_mode: str = os.environ.get("GMAIL_AUTH_MODE", "imap").strip().lower()

    # IMAP + App Password mode.
    email: str | None = os.environ.get("GMAIL_EMAIL") or None
    app_password: str | None = os.environ.get("GMAIL_APP_PASSWORD") or None
    imap_server: str = os.environ.get("GMAIL_IMAP_SERVER", "imap.gmail.com")
    imap_port: int = int(os.environ.get("GMAIL_IMAP_PORT", "993"))

    # OAuth 2.0 mode (Gmail API).
    client_id: str | None = os.environ.get("GMAIL_CLIENT_ID") or None
    client_secret: str | None = os.environ.get("GMAIL_CLIENT_SECRET") or None
    refresh_token: str | None = os.environ.get("GMAIL_REFRESH_TOKEN") or None

    # How often the background scheduler polls the mailbox for unread mail.
    poll_interval_seconds: int = int(os.environ.get("GMAIL_POLL_INTERVAL_SECONDS", "300"))

    # Sender whitelist: comma-separated email addresses. When set, ONLY unread
    # mails FROM these addresses are fetched and processed -- everything else
    # in the inbox is completely ignored (never read, never marked, never
    # imported). Empty/unset keeps the historical behaviour (all unread mails
    # with attachments are processed). Matching is on the address part only,
    # case-insensitive ("Rahul <rahul@acme.com>" matches "rahul@acme.com").
    allowed_senders: tuple[str, ...] = tuple(
        address.strip().lower()
        for address in os.environ.get("GMAIL_ALLOWED_SENDERS", "").split(",")
        if address.strip()
    )

    # Attachment-name filter: when set, ONLY attachments whose file name
    # STARTS WITH this prefix (case-insensitive) are extracted -- anything may
    # follow the prefix (e.g. "purchase_order" matches
    # "purchase_order_B-110826-6000045265-147.xlsx"). One matching file is
    # extracted alone; three or four matching files are ALL extracted. Other
    # attachments in the same email are ignored. Empty = extract every usable
    # Excel/PDF attachment (historical behaviour).
    attachment_prefix: str = os.environ.get("GMAIL_ATTACHMENT_PREFIX", "").strip().lower()

    # When set, every extracted attachment is imported UNDER THIS NAME instead
    # of its original file name (the original is still logged). If the value
    # has no file extension, the attachment's own extension is appended so an
    # .xlsx never turns into an extensionless file. Empty = keep original
    # names.
    save_attachment_as: str = os.environ.get("GMAIL_SAVE_ATTACHMENT_AS", "").strip()

    # Process emails even after a human has OPENED them (OAuth mode). The
    # historical `is:unread` filter silently skipped any mail someone read
    # before the poller got to it (e.g. the server was asleep at arrival and
    # the founder checked the inbox manually). Duplicate protection does NOT
    # depend on read-state: every processed email's id is recorded in the
    # database (`incoming_documents.email_message_id`, unique) and known ids
    # are skipped before importing -- so with the whitelist + today-only
    # rules this is strictly safer. Set to false to restore unread-only.
    process_read_emails: bool = (
        os.environ.get("GMAIL_PROCESS_READ_EMAILS", "true").strip().lower() == "true"
    )

    # Only process emails RECEIVED TODAY (business timezone, IST): an unread
    # mail left over from a previous day is never imported -- it simply stays
    # unread and untouched. Enforced both in the Gmail search query and again
    # per message (defense in depth). Set to false to process any unread mail
    # regardless of age (historical behaviour).
    today_only: bool = (
        os.environ.get("GMAIL_PROCESS_TODAY_ONLY", "true").strip().lower() == "true"
    )

    # Business rule: when a message has more than this many attachments, the
    # trailing ones (typically signature images/logos, per the purchase
    # team) are ignored.
    max_attachments_before_trim: int = 2

    def is_configured(self) -> bool:
        if self.auth_mode == "oauth":
            return bool(self.client_id and self.client_secret and self.refresh_token)
        return bool(self.email and self.app_password)


gmail_settings = GmailSettings()
