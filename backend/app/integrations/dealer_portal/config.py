"""Dealer Portal integration configuration, read from environment variables
(`backend/.env`). Kept separate from `backend/app/core/config.py` for the
same reason the WhatsApp integration is -- this integration must be able to
evolve, or be switched off entirely, without touching unrelated settings.
Same tiny-class-with-`os.environ.get` idiom, no pydantic-settings.

EVERYTHING here defaults to OFF/safe. With `DEALER_PORTAL_ENABLED` unset,
`push_service.request_push()` returns immediately and ProcureHub behaves
exactly as it does today.
"""

from __future__ import annotations

import os


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() == "true"


class DealerPortalSettings:
    # Master switch. false (the default) = no delta computed, no audit row
    # written, no network call -- ProcureHub is untouched.
    enabled: bool = _flag("DEALER_PORTAL_ENABLED")

    # Shadow mode: compute the full delta, write the DealerPortalPush audit
    # row with status=SHADOW, log exactly what WOULD be sent -- and make no
    # network call. Mirrors AI_SHADOW_MODE. Defaults to TRUE so that simply
    # turning DEALER_PORTAL_ENABLED on cannot accidentally write to a real
    # dealer account; going live is a deliberate second step
    # (DEALER_PORTAL_SHADOW=false).
    shadow: bool = _flag("DEALER_PORTAL_SHADOW", "true")

    base_url: str = os.environ.get(
        "DEALER_PORTAL_BASE_URL", "https://vagmine.mycentralpark.in/api/v1"
    ).strip().rstrip("/")

    # Comma-separated list of ACCOUNT KEYS. Each key names one Dealer Portal
    # dealer account and is the prefix for that account's own variables --
    # see `credentials.py` for the full per-account variable set.
    #
    #     DEALER_PORTAL_ACCOUNTS=BIJWASAN,MANSAROVAR,MARUTI,FORD
    account_keys: list[str] = [
        part.strip().upper()
        for part in os.environ.get("DEALER_PORTAL_ACCOUNTS", "").split(",")
        if part.strip()
    ]

    # Fallback TAT sent for every row (ProcureHub does not track a per-row
    # TAT). Overridable per account with DEALER_PORTAL_<KEY>_TAT_DAYS.
    tat_days: int = int(os.environ.get("DEALER_PORTAL_TAT_DAYS", "3") or 3)

    # How a part appearing more than once (in one vendor file, or across two
    # vendor rows of the same group) is collapsed before the CSV is written.
    # DP itself SUMS duplicates, which silently inflates stock if our parser
    # emits a part twice -- so we never inherit that. "max" is the
    # anti-inflation default: the largest single stated availability wins.
    # "sum" and "first" are available if the Founder decides otherwise.
    duplicate_strategy: str = (
        os.environ.get("DEALER_PORTAL_DUPLICATE_STRATEGY", "max").strip().lower()
    )

    # HTTP timeouts (seconds) for the login and upload calls.
    timeout_seconds: float = float(os.environ.get("DEALER_PORTAL_TIMEOUT_SECONDS", "60"))

    # A login-issued JWT is documented as valid ~28800s (8h). We refresh this
    # many seconds early rather than waiting for a 401. Ignored entirely when
    # a permanent token is configured for the account.
    token_refresh_margin_seconds: int = int(
        os.environ.get("DEALER_PORTAL_TOKEN_REFRESH_MARGIN_SECONDS", "300")
    )

    # Retry sweep for FAILED pushes (scheduler). 0 disables the sweep.
    retry_interval_minutes: int = int(
        os.environ.get("DEALER_PORTAL_RETRY_INTERVAL_MINUTES", "30")
    )
    retry_max_attempts: int = int(os.environ.get("DEALER_PORTAL_RETRY_MAX_ATTEMPTS", "5"))


dealer_portal_settings = DealerPortalSettings()
