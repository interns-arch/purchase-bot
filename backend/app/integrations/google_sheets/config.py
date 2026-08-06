"""Google Sheets Sync configuration, read from environment variables
(`backend/.env`). Same tiny-class idiom as the WhatsApp/Gmail integrations.

`GOOGLE_SERVICE_ACCOUNT_JSON` accepts EITHER a path to a service account key
file OR the key's JSON contents inline (handy for platforms like Render
where a multi-line file is awkward to configure but a single env var is
easy) -- detected by whether the value parses as JSON."""

from __future__ import annotations

import json
import os


class GoogleSheetsSettings:
    enabled: bool = (
        os.environ.get("ENABLE_GOOGLE_SHEETS_SYNC", "false").strip().lower() == "true"
    )
    sheet_id: str | None = os.environ.get("GOOGLE_SHEET_ID") or None
    project_id: str | None = os.environ.get("GOOGLE_PROJECT_ID") or None
    _service_account_json: str | None = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None

    def is_configured(self) -> bool:
        return bool(self.sheet_id and self._service_account_json)

    def load_service_account_info(self) -> dict:
        """Raises `ValueError` with a clear message if the credential is
        missing or malformed -- callers turn this into a logged failure,
        never a crash."""
        if not self._service_account_json:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured.")

        raw = self._service_account_json.strip()
        if raw.startswith("{"):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc

        # Otherwise treat it as a path to a service account key file.
        try:
            with open(raw, encoding="utf-8") as key_file:
                return json.load(key_file)
        except OSError as exc:
            raise ValueError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON does not look like inline JSON and "
                f"'{raw}' could not be opened as a key file: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"'{raw}' is not a valid service account JSON key file: {exc}") from exc


google_sheets_settings = GoogleSheetsSettings()
