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

    @property
    def graph_api_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"


whatsapp_settings = WhatsAppSettings()
