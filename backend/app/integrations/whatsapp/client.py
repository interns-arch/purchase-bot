"""HTTP client for the Meta WhatsApp Cloud API (Graph API). Two calls only:
resolve a media id to a download URL, then download the bytes from that URL
-- both require the same Bearer access token. Wrapped in a small retry with
exponential backoff since this runs inside a background task, not a
request/response cycle, so a blocking sleep between attempts is harmless."""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

import httpx

from backend.app.integrations.whatsapp.config import WhatsAppSettings
from core.logging_setup import get_logger

logger = get_logger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1, 2, 4)
_T = TypeVar("_T")


class WhatsAppClientError(Exception):
    """Raised when a Graph API call fails after all retry attempts."""


class WhatsAppNotConfiguredError(Exception):
    """Raised when the client is used without a configured access token."""


def _with_retries(description: str, call: Callable[[], _T]) -> _T:
    last_exc: Exception | None = None
    for attempt, backoff in enumerate((0, *_RETRY_BACKOFF_SECONDS), start=1):
        if backoff:
            time.sleep(backoff)
        try:
            return call()
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.warning(
                "%s failed (attempt %d/%d): %s", description, attempt, _RETRY_ATTEMPTS + 1, exc
            )
    raise WhatsAppClientError(f"{description} failed after retries.") from last_exc


class WhatsAppClient:
    def __init__(self, settings: WhatsAppSettings, *, timeout: float = 30.0):
        self._settings = settings
        self._timeout = timeout

    def _require_access_token(self) -> str:
        if not self._settings.access_token:
            raise WhatsAppNotConfiguredError(
                "WHATSAPP_ACCESS_TOKEN is not configured -- cannot call the Graph API."
            )
        return self._settings.access_token

    def get_media_url(self, media_id: str) -> dict[str, Any]:
        """Resolves a media id to its (short-lived) download URL, mime type,
        and file size. https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media"""
        access_token = self._require_access_token()
        url = f"{self._settings.graph_api_base_url}/{media_id}"

        def _call() -> dict[str, Any]:
            with httpx.Client(timeout=self._timeout) as client:
                logger.info(
                    "HTTP GET media metadata (media_id=%s, timeout=%ss) -- awaiting Graph API...",
                    media_id,
                    self._timeout,
                )
                response = client.get(
                    url, headers={"Authorization": f"Bearer {access_token}"}
                )
                logger.info(
                    "HTTP GET media metadata returned status %s (media_id=%s)",
                    response.status_code,
                    media_id,
                )
                response.raise_for_status()
                return response.json()

        return _with_retries(f"get_media_url({media_id})", _call)

    def download_media(self, media_url: str) -> bytes:
        """Downloads the actual file bytes from the URL returned by
        `get_media_url` -- Meta requires the same Bearer token here too."""
        access_token = self._require_access_token()

        def _call() -> bytes:
            with httpx.Client(timeout=self._timeout) as client:
                logger.info(
                    "HTTP GET media bytes (timeout=%ss) -- awaiting download...", self._timeout
                )
                response = client.get(
                    media_url, headers={"Authorization": f"Bearer {access_token}"}
                )
                logger.info(
                    "HTTP GET media bytes returned status %s (%s bytes)",
                    response.status_code,
                    response.headers.get("content-length", "unknown"),
                )
                response.raise_for_status()
                return response.content

        return _with_retries(f"download_media({media_url})", _call)

    def send_text_message(self, to: str, body: str) -> None:
        """Send a plain-text WhatsApp message back to `to` (an inbound sender
        number). Used only for the routing command prompts/confirmations --
        never to deliver business documents to a vendor. Requires
        `WHATSAPP_PHONE_NUMBER_ID`. Wrapped in the same retry/backoff as the
        media calls since it also runs inside a background task.
        https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages"""
        access_token = self._require_access_token()
        if not self._settings.phone_number_id:
            raise WhatsAppNotConfiguredError(
                "WHATSAPP_PHONE_NUMBER_ID is not configured -- cannot send a message."
            )

        url = f"{self._settings.graph_api_base_url}/{self._settings.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }

        def _call() -> None:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=payload,
                )
                response.raise_for_status()

        _with_retries(f"send_text_message({to})", _call)

    def get_phone_number_info(self) -> dict[str, Any]:
        """Used only by the interactive "Test Connection" action on the
        Integration Status page -- a single attempt, deliberately NOT
        wrapped in `_with_retries` (that adds up to 7s of blocking sleep,
        which would make a button click feel broken). Confirms the access
        token and phone number id are both valid by asking Meta for the
        number's own profile. https://developers.facebook.com/docs/whatsapp/cloud-api/reference/phone-numbers"""
        access_token = self._require_access_token()
        if not self._settings.phone_number_id:
            raise WhatsAppNotConfiguredError(
                "WHATSAPP_PHONE_NUMBER_ID is not configured -- cannot call the Graph API."
            )

        url = f"{self._settings.graph_api_base_url}/{self._settings.phone_number_id}"
        with httpx.Client(timeout=self._timeout) as client:
            response = client.get(
                url,
                params={"fields": "verified_name,display_phone_number,quality_rating"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            return response.json()
