"""One-time local helper: mint a Google refresh token carrying BOTH scopes
this app needs (Gmail read/send + Google Sheets). Run it on your own machine,
never on the server:

    cd D:\\Downloads\\pythonscript
    venv\\Scripts\\python.exe generate_google_token.py

Prerequisites:
- `oauth_client.json` in this folder: the OAuth "Desktop app" client JSON
  downloaded from Google Cloud Console -> APIs & Services -> Credentials
  (same client as GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET).
- A browser window will open; sign in with the inbox account
  (prateekjain1593@gmail.com) and approve BOTH permissions.

Afterwards, put the printed refresh token into GMAIL_REFRESH_TOKEN in
backend/.env AND in the Render environment, then redeploy. Also make sure
the Gmail API and Google Sheets API are ENABLED for the same Google Cloud
project the client belongs to.

Never commit oauth_client.json or the printed token.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

flow = InstalledAppFlow.from_client_secrets_file(
    "oauth_client.json",
    SCOPES,
)

credentials = flow.run_local_server(
    port=0,
    access_type="offline",
    prompt="consent",
)

print("\n====================================")
print("NEW REFRESH TOKEN:")
print(credentials.refresh_token)
print("====================================")
