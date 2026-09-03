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
    # When an import FAILS (or needs review), send the original file back to
    # the admin number(s) so it can be opened straight from the chat -- the
    # file that failed is exactly the one the Founder needs to look at, and
    # the server's copy may be cleared by a later restart. false = only the
    # failure message is sent (the file stays downloadable on the web while
    # it exists).
    send_failed_file: bool = (
        os.environ.get("WHATSAPP_SEND_FAILED_FILE", "true").strip().lower() == "true"
    )

    # Send each generated Purchase Order on WhatsApp (the Founder's rule):
    # the vendor's registered number (their OWN PO only), every registered
    # purchase-team member, the number that originally sent the order, and
    # the admin number(s). WHATSAPP_SEND_PO=false disables all of it;
    # WHATSAPP_SEND_PO_TO_VENDOR=false keeps it internal (team + admin).
    send_po: bool = (
        os.environ.get("WHATSAPP_SEND_PO", "true").strip().lower() == "true"
    )
    send_po_to_vendor: bool = (
        os.environ.get("WHATSAPP_SEND_PO_TO_VENDOR", "true").strip().lower() == "true"
    )

    # The COMBINED allocation workbook (one sheet per customer order).
    # Default changed to false on 25 Aug 2026: the Founder found that sheet
    # hard to read and asked for one message per vendor instead (below).
    # Set true to receive both.
    send_allocation_report: bool = (
        os.environ.get("WHATSAPP_SEND_ALLOCATION_REPORT", "false").strip().lower() == "true"
    )

    # ONE MESSAGE PER VENDOR after an allocation batch (Founder, 25 Aug 2026:
    # "Purchase from Ess aay ... Purchase from Northend ..."), answering
    # "what do we buy from this vendor?" -- see whatsapp/vendor_purchase_output.py.
    allocation_per_vendor: bool = (
        os.environ.get("WHATSAPP_ALLOCATION_PER_VENDOR", "true").strip().lower() == "true"
    )

    # A vendor with more purchase lines than this gets a small Excel instead
    # of an unreadably long text message ("details in excel or text").
    vendor_message_max_lines: int = int(
        os.environ.get("WHATSAPP_VENDOR_MESSAGE_MAX_LINES", "20")
    )

    # ONE EXCEL, ONE SHEET PER VENDOR after an allocation batch (Founder,
    # 1 Sep 2026): instead of a separate text message per vendor, a single
    # workbook is sent whose tabs are named after the vendors ("Bijwasan",
    # "Northend", "Jaipur"), each listing that vendor's parts with the
    # CUSTOMER REQUESTED and VENDOR AVAILABLE quantities alongside what was
    # allocated -- detail a text message cannot carry. A leading Summary tab
    # indexes the vendors.
    #
    # Internal only: the workbook shows every vendor we buy from and at what
    # quantity, so it goes to the founder/admin number(s) and the registered
    # purchase team -- the same recipients as before -- and never to a vendor.
    #
    # false restores the per-vendor text messages exactly. The texts are also
    # used automatically as a FALLBACK if the workbook cannot be built or
    # delivered, so a spreadsheet problem never costs the Founder the
    # purchase instructions.
    vendor_workbook: bool = (
        os.environ.get("WHATSAPP_VENDOR_WORKBOOK", "true").strip().lower() == "true"
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
    # 09:30 IST (Founder, 19 Aug 2026 -- was 09:00).
    daily_request_time: str = os.environ.get("WHATSAPP_DAILY_REQUEST_TIME", "09:30").strip()
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

    # --- SALES TEAM stock checks (Founder, 3 Sep 2026) -------------------
    # A registered sales number asks "is this part available?" and gets
    # availability + quantity back -- never the vendor or the price. The
    # answer is held as a QUOTE so one word ("confirm Karol Bagh") turns it
    # into a real order, without retyping the parts.
    #
    # How long a quote can still be confirmed. An hour-old answer is not a
    # safe basis for an order: stock moves. 0 = never expires (not advised).
    sales_quote_ttl_minutes: int = int(
        os.environ.get("WHATSAPP_SALES_QUOTE_TTL_MINUTES", "60")
    )
    # Past the TTL the quote is not thrown away: on "confirm" the stock is
    # looked up AGAIN, the fresh quantities are shown, and the member is
    # asked to confirm those -- so they never retype the parts. This is the
    # HARD ceiling (hours) beyond which even that is abandoned and they are
    # asked to send the parts again. 0 = no ceiling.
    sales_quote_max_age_hours: float = float(
        os.environ.get("WHATSAPP_SALES_QUOTE_MAX_AGE_HOURS", "24")
    )
    # OPTIONAL single reminder: if a quote is still unconfirmed after this
    # many minutes, ask ONCE "should this be placed as an order?" -- never
    # repeated. 0 (the default) disables it entirely: with ten people asking
    # all day, unprompted nudges become noise, so this is opt-in.
    sales_nudge_minutes: int = int(
        os.environ.get("WHATSAPP_SALES_NUDGE_MINUTES", "0")
    )
    # How long the "same customer as last time?" suggestion stays on offer
    # (hours). A sales person serves MANY customers, so the suggestion is
    # always SHOWN and never applied silently -- this only stops the bot
    # proposing yesterday's customer for today's order. 0 = never suggest.
    sales_customer_memory_hours: float = float(
        os.environ.get("WHATSAPP_SALES_CUSTOMER_MEMORY_HOURS", "12")
    )

    @property
    def graph_api_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"


whatsapp_settings = WhatsAppSettings()
