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

    # The host MOVED (4 Sep 2026). The old `vagmine.mycentralpark.in` no
    # longer completes a TLS handshake at all -- it is dead, not merely
    # renamed. The dealer portal UI now lives at cartrend.vagminetech.com and
    # its API at the base below (confirmed from the site's own bundle and a
    # successful login).
    base_url: str = os.environ.get(
        "DEALER_PORTAL_BASE_URL", "https://vagmine.vagminetech.com/api/v1"
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

    # NEVER PUSH THESE (Harun + Founder, call of 4 Sep 2026).
    #
    # Bijwasan, Mansarovar and Jaipur stock is PULLED OUT OF Dealer Portal's
    # own ERP in the first place -- Harun exports it there, then sends it on
    # WhatsApp. Pushing it back would double the same stock inside DP, which
    # is exactly what he flagged on the call: "dealer portal pe apna stock
    # already hai... wahi se main nikal ke deta hoon aapko."
    #
    # This REVERSES spec section 8(a), which had said own-stock vendors get a
    # DP account and are pushed like any other vendor. They are not.
    #
    # The account list is opt-in already, so an unlisted vendor is never
    # pushed. This is the second lock: even if one of these names is added to
    # a DEALER_PORTAL_<KEY>_VENDORS group by mistake, it is dropped and
    # logged. Matched as whole words, same rule as OWN_STOCK_VENDOR_NAME.
    exclude_vendors: list[str] = [
        part.strip()
        for part in os.environ.get(
            "DEALER_PORTAL_EXCLUDE_VENDORS",
            "Bijvasan, Bijwasan, Bijwashan, Mansarovar, Mansarover, Maansarovar, Jaipur",
        ).split(",")
        if part.strip()
    ]

    # ONE FILE INSTEAD OF THREE ENV LINES PER VENDOR.
    #
    # Every external vendor needs its own DP dealer account, and writing
    # USERNAME / PASSWORD / VENDORS into .env for each one does not scale
    # past a handful. Point this at a JSON file instead and the whole vendor
    # list lives in one place -- adding a vendor is one JSON object, not an
    # .env edit.
    #
    # STILL NOT THE DATABASE. The spec's rule (section 7.4) is that
    # credentials never sit in the database in plaintext and never reach a
    # log; a file read at runtime honours both, and it is what Docker
    # secrets / a mounted volume are for. Give it 0600 permissions and keep
    # it out of git.
    #
    # Env-defined accounts and file-defined accounts can both be used; on a
    # duplicate key the ENV one wins, so a single account can always be
    # overridden without editing the file.
    accounts_file: str | None = (
        os.environ.get("DEALER_PORTAL_ACCOUNTS_FILE", "").strip() or None
    )

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
