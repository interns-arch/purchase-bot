"""Handing a quoted order back to the sales bot.

The sales bot can always PULL: `GET /api/advance-orders/{id}` has returned
the full picture since day one, and still does. This module adds the PUSH, so
the bot hears about a quote the moment a vendor answers instead of on its next
poll.

SAFETY -- THE SAME CONTRACT THE DEALER PORTAL PUSH KEEPS
--------------------------------------------------------
This runs inside the WhatsApp reply path and the scheduler tick. Both are
places where an exception would strand a vendor conversation, so:

  1. `ADVANCE_ORDER_CALLBACK_URL` is blank by default. Blank, `notify()`
     returns on its first line: no request, no thread, nothing.
  2. Everything is wrapped. A timeout, a 500 from the sales bot, a DNS
     failure or a bug in here is logged and swallowed. It can never fail a
     vendor reply, block a line from being answered, or roll back a quote.
  3. Retries are bounded and backed off, and they happen on a worker thread,
     so nobody waits on the sales bot being up.
  4. SHADOW mode logs the exact body that would be posted and sends nothing.

WHAT IS SENT
------------
The same JSON `GET /api/advance-orders/{id}` returns, wrapped with the event
name, so the bot has one shape to parse:

    {"event": "quote.updated" | "order.settled", "order": { ...order_out... }}

Authentication: the shared secret goes in `X-Api-Key`, and an HMAC-SHA256 of
the exact body bytes in `X-Signature` (hex), so the bot can verify the call
really came from ProcureHub rather than trusting an open endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

from backend.app.advance_orders.config import advance_order_settings as cfg
from core.logging_setup import get_logger

logger = get_logger(__name__)

EVENT_QUOTE_UPDATED = "quote.updated"
EVENT_ORDER_SETTLED = "order.settled"


def _http():
    """httpx, imported lazily. It is an explicit dependency
    (backend/requirements.txt) and the WhatsApp client already uses it --
    unlike `requests`, which is only present because Google's libraries happen
    to pull it in."""
    import httpx

    return httpx


def _signature(body: bytes) -> str | None:
    if not cfg.callback_secret:
        return None
    return hmac.new(cfg.callback_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_with_retries(body: bytes, headers: dict[str, str]) -> None:
    httpx = _http()
    delay = 2.0
    for attempt in range(1, cfg.callback_max_attempts + 1):
        try:
            response = httpx.post(
                cfg.callback_url,
                content=body,
                headers=headers,
                timeout=cfg.callback_timeout_seconds,
            )
            if 200 <= response.status_code < 300:
                logger.info("Advance order callback delivered (attempt %d).", attempt)
                return
            # 4xx other than 429 will not improve by repeating it.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                logger.warning(
                    "Advance order callback refused with HTTP %s -- not retrying.",
                    response.status_code,
                )
                return
            logger.warning(
                "Advance order callback attempt %d got HTTP %s.", attempt, response.status_code
            )
        except Exception as exc:  # noqa: BLE001 -- network layer; never propagate
            logger.warning("Advance order callback attempt %d failed: %s", attempt, exc)
        if attempt < cfg.callback_max_attempts:
            time.sleep(delay)
            delay *= 2
    logger.error(
        "Advance order callback gave up after %d attempts. The sales bot can still "
        "read the order with GET /api/advance-orders/{id}.",
        cfg.callback_max_attempts,
    )


def notify(event: str, order_payload: dict) -> None:
    """Tell the sales bot. Never raises, never blocks the caller."""
    try:
        if not cfg.callback_url:
            return
        body = json.dumps({"event": event, "order": order_payload}, default=str).encode("utf-8")

        if cfg.callback_shadow:
            logger.info(
                "Advance order callback SHADOW -- would POST %s to %s: %s",
                event,
                cfg.callback_url,
                body.decode("utf-8")[:1500],
            )
            return

        headers = {"Content-Type": "application/json"}
        if cfg.callback_secret:
            headers["X-Api-Key"] = cfg.callback_secret
            signature = _signature(body)
            if signature:
                headers["X-Signature"] = signature

        threading.Thread(
            target=_post_with_retries,
            args=(body, headers),
            name="advance-order-callback",
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 -- an output must never affect the conversation
        logger.exception("Advance order callback could not be started. The order is unaffected.")
