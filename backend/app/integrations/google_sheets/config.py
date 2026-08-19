"""Google Sheets Sync configuration, read from environment variables
(`backend/.env`). Same tiny-class idiom as the WhatsApp/Gmail integrations.

Authentication reuses the SAME OAuth 2.0 credentials as the Gmail
integration -- `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` /
`GMAIL_REFRESH_TOKEN`, delegated to `gmail_settings` so there's a single
source of truth. There is no longer a separate Google service account
(`GOOGLE_SERVICE_ACCOUNT_JSON` has been removed).

The reused refresh token must have been granted the Google Sheets scope
(`https://www.googleapis.com/auth/spreadsheets`) IN ADDITION to Gmail's
scope when it was minted -- see DEPLOYMENT.md's Gmail/Google Sheets setup.
"""

from __future__ import annotations

import os

from backend.app.integrations.gmail.config import gmail_settings

# The OAuth scope Google Sheets access requires. The shared refresh token
# must have been consented for this scope as well as Gmail's.
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class GoogleSheetsSettings:
    enabled: bool = (
        os.environ.get("ENABLE_GOOGLE_SHEETS_SYNC", "false").strip().lower() == "true"
    )
    sheet_id: str | None = os.environ.get("GOOGLE_SHEET_ID") or None
    project_id: str | None = os.environ.get("GOOGLE_PROJECT_ID") or None

    # Daily reset: every morning (IST) vendor-code worksheets whose vendor
    # has NOT uploaded stock TODAY are removed, so the Sheet only ever shows
    # same-day inventory. Hand-made tabs (titles that aren't a Vendor Code)
    # are never touched. Runs only when the sync itself is enabled.
    daily_reset_enabled: bool = (
        os.environ.get("GOOGLE_SHEETS_DAILY_RESET_ENABLED", "true").strip().lower() == "true"
    )
    # 09:15 IST (Founder, 18 Aug 2026 -- was 06:00): late enough that the
    # 09:00 morning stock request has gone out, so a vendor who replies
    # promptly keeps their tab. Sheet-only: clearing a tab never touches the
    # vendor's stock in the database, so an order arriving at 08:30 still
    # allocates against yesterday's stock exactly as before.
    daily_reset_time: str = os.environ.get("GOOGLE_SHEETS_DAILY_RESET_TIME", "09:15").strip()

    # OAuth credentials are shared with the Gmail integration (same Google
    # account) -- read through `gmail_settings` rather than re-reading the
    # env, so both integrations can never drift apart.
    @property
    def client_id(self) -> str | None:
        return gmail_settings.client_id

    @property
    def client_secret(self) -> str | None:
        return gmail_settings.client_secret

    @property
    def refresh_token(self) -> str | None:
        return gmail_settings.refresh_token

    def is_configured(self) -> bool:
        return bool(
            self.sheet_id and self.client_id and self.client_secret and self.refresh_token
        )


google_sheets_settings = GoogleSheetsSettings()
