"""WhatsApp Cloud API configuration, read from environment variables
(`backend/.env`). Kept separate from `backend/app/core/config.py` on
purpose -- same reasoning as auth being separate from business config: this
integration should be able to evolve (or be swapped out entirely) without
touching unrelated settings. Same tiny-class-with-`os.environ.get` idiom,
no pydantic-settings dependency."""

from __future__ import annotations

import os


class WhatsAppSettings:
    # ENABLE_WHATSAPP_AUTOMATION is accepted as a fallback alias for
    # WHATSAPP_ENABLED (same alias pattern as everywhere else in this file).
    enabled: bool = (
        os.environ.get("WHATSAPP_ENABLED", os.environ.get("ENABLE_WHATSAPP_AUTOMATION", "false"))
        .strip()
        .lower()
        == "true"
    )
    graph_api_version: str = os.environ.get("WHATSAPP_GRAPH_API_VERSION", "v23.0")
    access_token: str | None = os.environ.get("WHATSAPP_ACCESS_TOKEN") or None
    phone_number_id: str | None = os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or None
    # META_BUSINESS_ID is accepted as a fallback alias for
    # WHATSAPP_BUSINESS_ACCOUNT_ID.
    business_account_id: str | None = (
        os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID") or os.environ.get("META_BUSINESS_ID") or None
    )
    # META_APP_ID / META_APP_SECRET are accepted as fallback aliases for
    # WHATSAPP_APP_ID / WHATSAPP_APP_SECRET -- both names refer to the same
    # Meta App (WhatsApp is one product within a Meta App).
    app_id: str | None = os.environ.get("WHATSAPP_APP_ID") or os.environ.get("META_APP_ID") or None
    # WHATSAPP_WEBHOOK_SECRET is accepted as a fallback alias for
    # WHATSAPP_APP_SECRET -- both name the same value used to verify
    # X-Hub-Signature-256 on incoming webhook posts.
    app_secret: str | None = (
        os.environ.get("WHATSAPP_APP_SECRET")
        or os.environ.get("META_APP_SECRET")
        or os.environ.get("WHATSAPP_WEBHOOK_SECRET")
        or None
    )

    # Meta's webhook verification handshake (GET /webhook) checks
    # `hub.verify_token` against this. `WHATSAPP_WEBHOOK_VERIFY_TOKEN` is
    # Meta's documented name for it; `WHATSAPP_VERIFY_TOKEN` is accepted as a
    # fallback since both were requested.
    webhook_verify_token: str | None = (
        os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
        or os.environ.get("WHATSAPP_VERIFY_TOKEN")
        or None
    )
    webhook_callback_url: str | None = os.environ.get("WHATSAPP_WEBHOOK_CALLBACK_URL") or None

    # Founder/admin destination(s) for outbound documents, notifications and
    # daily summaries. COMMA-SEPARATED for several numbers (e.g.
    # "919876543210, 919812345678") -- every listed number receives every
    # founder-facing message, and each may text "send reminder" / manage the
    # contact registry. WhatsApp international format without '+'. If unset,
    # founder-facing sends are skipped (logged, never an error). Reuses the
    # same WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID -- no separate
    # auth. `admin_phone_number` stays as the FIRST number for any legacy
    # single-recipient use.
    admin_phone_numbers: list[str] = [
        part.strip()
        for part in os.environ.get("WHATSAPP_ADMIN_PHONE_NUMBER", "").split(",")
        if part.strip()
    ]
    admin_phone_number: str | None = admin_phone_numbers[0] if admin_phone_numbers else None

    # Mirror every UI toast notification (import results, Sheet/allocation
    # outcomes, failures) as a WhatsApp text to WHATSAPP_ADMIN_PHONE_NUMBER
    # -- the Founder sees the same events the web UI shows without keeping it
    # open. Events flagged web-only (e.g. "the workbook was sent to
    # WhatsApp") are never mirrored. Requires the admin number above; set
    # WHATSAPP_FORWARD_NOTIFICATIONS=false to turn the mirror off.
    forward_notifications: bool = (
        os.environ.get("WHATSAPP_FORWARD_NOTIFICATIONS", "true").strip().lower() == "true"
    )

    # Send the consolidated Vendor_Inventory.xlsx to WhatsApp after vendor
    # imports. false = keep the chat text-only; the workbook stays available
    # on the web (Vendor Inventory -> Download Workbook) and the Google Sheet
    # is updated as usual.
    send_workbook: bool = (
        os.environ.get("WHATSAPP_SEND_WORKBOOK", "true").strip().lower() == "true"
    )
    # Send allocation report workbooks to WhatsApp after automatic vendor
    # selection. false = allocations still run and are visible on the web
    # (Vendor Comparison / exports); only the WhatsApp file is skipped.
    send_allocation_report: bool = (
        os.environ.get("WHATSAPP_SEND_ALLOCATION_REPORT", "true").strip().lower() == "true"
    )

    # When several vendor files arrive in one WhatsApp batch, each successful
    # import requests the consolidated workbook -- this debounce coalesces
    # those requests so ONE final workbook is sent after imports have been
    # quiet for this many seconds (0 = send immediately per import, the old
    # behaviour).
    workbook_debounce_seconds: float = float(
        os.environ.get("WHATSAPP_WORKBOOK_DEBOUNCE_SECONDS", "20")
    )

    # Automatic vendor selection for imported customer orders (Founder
    # requirement, "Combined ZIP" mode): orders are collected while a batch is
    # arriving; once order imports have been quiet for this many seconds, the
    # engine auto-selects vendors for every pending order IN ARRIVAL ORDER
    # (each order consumes stock before the next) and ONE ZIP containing every
    # order's allocation report is sent to WHATSAPP_ADMIN_PHONE_NUMBER.
    # 0 = process each order the moment it imports (one ZIP per order).
    # Set WHATSAPP_AUTO_ALLOCATION_ENABLED=false to turn the automation off
    # entirely (the manual Auto-Select button keeps working either way).
    auto_allocation_enabled: bool = (
        os.environ.get("WHATSAPP_AUTO_ALLOCATION_ENABLED", "true").strip().lower() == "true"
    )
    allocation_batch_debounce_seconds: float = float(
        os.environ.get("WHATSAPP_ALLOCATION_BATCH_DEBOUNCE_SECONDS", "20")
    )

    # Master switch for the NUMBER REGISTRY fast path: registered vendor/
    # customer numbers upload files directly (no command/caption). Set to
    # false to suspend it -- registrations are KEPT in the database, but
    # every number behaves like an unregistered sender (classic command/
    # caption flow) until re-enabled. The Founder's "register" contact-list
    # flow keeps working either way, so the registry can be maintained while
    # suspended.
    registry_enabled: bool = (
        os.environ.get("WHATSAPP_NUMBER_REGISTRY_ENABLED", "true").strip().lower() == "true"
    )

    # --- Daily vendor stock automation (number registry) -----------------
    # Morning stock request: at this IST time, the PRE-APPROVED template
    # below is sent to every registered vendor number ("please share your
    # stock"). Disabled by default -- enable ONLY after the template is
    # approved in the Meta dashboard, or every send will fail.
    daily_request_enabled: bool = (
        os.environ.get("WHATSAPP_DAILY_REQUEST_ENABLED", "false").strip().lower() == "true"
    )
    daily_request_time: str = os.environ.get("WHATSAPP_DAILY_REQUEST_TIME", "09:00").strip()
    # Daily participation summary to WHATSAPP_ADMIN_PHONE_NUMBER at this IST
    # time: "Received: X of Y vendors. Pending: ..." -- plain text, no
    # template needed (the admin messages the bot daily).
    daily_summary_enabled: bool = (
        os.environ.get("WHATSAPP_DAILY_SUMMARY_ENABLED", "true").strip().lower() == "true"
    )
    daily_summary_time: str = os.environ.get("WHATSAPP_DAILY_SUMMARY_TIME", "11:00").strip()
    # Optional automatic reminder to STILL-PENDING vendors at this IST time
    # (e.g. "11:30"). Empty = disabled; the admin's manual "send reminder"
    # text works either way.
    auto_reminder_time: str = os.environ.get("WHATSAPP_AUTO_REMINDER_TIME", "").strip()
    # Meta template names (must be approved in the Meta dashboard) + their
    # language code. The reminder template defaults to the stock-request
    # template -- one approved template can serve both.
    stock_request_template: str = os.environ.get(
        "WHATSAPP_STOCK_REQUEST_TEMPLATE", "stock_request"
    ).strip()
    reminder_template: str = (
        os.environ.get("WHATSAPP_REMINDER_TEMPLATE", "").strip()
        or os.environ.get("WHATSAPP_STOCK_REQUEST_TEMPLATE", "stock_request").strip()
    )
    template_language: str = os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "en").strip()

    # Conversation grouping window (minutes): after a sender's routing command
    # and (for vendor files) supplied vendor name, FURTHER files from the same
    # number within this window are grouped automatically -- same command,
    # same vendor -- with no "please send a command" / "which vendor?"
    # re-asking per file. Expired window -> fresh conversation as before.
    # 0 disables grouping (legacy per-file behaviour).
    grouping_window_minutes: float = float(
        os.environ.get("WHATSAPP_GROUPING_WINDOW_MINUTES", "10")
    )

    @property
    def graph_api_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"


whatsapp_settings = WhatsAppSettings()
