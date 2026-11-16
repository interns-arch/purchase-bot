"""Mint the ONE Google refresh token that Gmail automation and Google Sheets
Sync share, then prove it works before you paste it anywhere.

    python -m backend.scripts.generate_google_token
    python -m backend.scripts.generate_google_token --client-json oauth_client.json

A browser opens; sign in as the mailbox account (e.g. itsupport@cartrends.net)
and allow both permissions. The script prints GMAIL_REFRESH_TOKEN, then
refreshes it once and shows the scopes Google actually granted.

WHY THE TOKEN KEPT DYING EVERY WEEK
-----------------------------------
A Google OAuth app whose consent screen is "External" and still in "Testing"
has its refresh tokens expired by Google after 7 days -- every re-mint only
buys another week, and the symptom is exactly:

    invalid_grant: Token has been expired or revoked.

cartrends.net is a Google Workspace domain, so set the consent screen's User
type to INTERNAL (Google Cloud console -> APIs & Services -> OAuth consent
screen). Internal apps have no 7-day expiry and no test-user list. Mint the
token AFTER that change -- a token minted while the app was in Testing keeps
its 7-day life.

Client ID / secret come from --client-json (the Desktop-app JSON downloaded
from Credentials), or else from GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in
backend/.env or the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Both integrations share this one token, so it must carry both scopes.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]


def client_config(path: Path | None) -> dict:
    if path is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "installed" not in data:
            raise SystemExit(f"{path} is not a Desktop-app OAuth client JSON (no 'installed' key).")
        return data
    client_id = (os.environ.get("GMAIL_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GMAIL_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "No client credentials. Pass --client-json oauth_client.json, or set "
            "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET."
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def verify(client_id: str, client_secret: str, refresh_token: str) -> None:
    """Refresh once, exactly as the app will, and show what was granted."""
    import httpx

    token = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    ).json()
    if "access_token" not in token:
        raise SystemExit(f"The new token does NOT work: {token}")
    info = httpx.get(
        "https://oauth2.googleapis.com/tokeninfo",
        params={"access_token": token["access_token"]},
        timeout=30,
    ).json()
    granted = set((info.get("scope") or "").split())
    print(f"  signed in as: {info.get('email', '(not shared)')}")
    for scope in SCOPES:
        print(f"  {'OK     ' if scope in granted else 'MISSING'} {scope}")
    if not set(SCOPES) <= granted:
        raise SystemExit(
            "A permission was not granted. Run again and tick BOTH boxes on the consent screen."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint the shared Gmail + Sheets refresh token.")
    parser.add_argument("--client-json", type=Path, default=None)
    args = parser.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    config = client_config(args.client_json)
    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    # offline + consent: guarantees Google returns a refresh token even for an
    # account that has approved this app before.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise SystemExit("Google returned no refresh token. Run again.")

    installed = config["installed"]
    print("\nChecking the new token...")
    verify(installed["client_id"], installed["client_secret"], credentials.refresh_token)

    print("\nPut these three in the backend's environment (same values for Gmail and Sheets):\n")
    print(f"GMAIL_CLIENT_ID={installed['client_id']}")
    print(f"GMAIL_CLIENT_SECRET={installed['client_secret']}")
    print(f"GMAIL_REFRESH_TOKEN={credentials.refresh_token}")
    print("\nThen restart the backend so it reads them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
