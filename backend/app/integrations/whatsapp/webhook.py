"""Webhook security -- Meta's documented contract: every POST to the
webhook is signed with `X-Hub-Signature-256`, an HMAC-SHA256 of the raw
request body keyed with the app secret. https://developers.facebook.com/docs/graph-api/webhooks/getting-started#validate-payloads"""

from __future__ import annotations

import hashlib
import hmac

_SIGNATURE_PREFIX = "sha256="


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    if not signature_header or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False

    provided_digest = signature_header[len(_SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected_digest, provided_digest)
