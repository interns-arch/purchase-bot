"""Advance-order settings, read from the environment (`backend/.env`). Same
tiny-class-with-`os.environ.get` idiom as the WhatsApp settings."""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


class AdvanceOrderSettings:
    # MASTER SWITCH. Off: the API answers 503 and no vendor is messaged.
    enabled: bool = os.environ.get("ADVANCE_ORDERS_ENABLED", "false").strip().lower() == "true"
    # The shared key the sales bot sends in X-Api-Key (PROCUREHUB_API_KEY there).
    # Blank = every call is refused, even when switched on.
    api_key: str = (os.environ.get("ADVANCE_ORDER_API_KEY") or "").strip()
    # How long one vendor gets to answer before the next one is asked.
    vendor_wait_minutes: int = max(5, _int("ADVANCE_ORDER_VENDOR_WAIT_MINUTES", 120))
    # Vendors are only messaged inside these IST hours ("HH:MM-HH:MM"); a
    # question due outside them waits for the window to open.
    vendor_hours: str = (os.environ.get("ADVANCE_ORDER_VENDOR_HOURS") or "09:30-19:00").strip()
    # A Meta-approved template for the vendor question -- needed for a vendor
    # who has not messaged in 24 h. Body params: {{1}} brand, {{2}} the parts
    # ("16510M68K10 x10; 2630002752 x4"), {{3}} needed-by date. Blank = plain text.
    vendor_template: str = (os.environ.get("ADVANCE_ORDER_VENDOR_TEMPLATE") or "").strip()
    template_language: str = (os.environ.get("ADVANCE_ORDER_TEMPLATE_LANGUAGE") or "en").strip()
    # The vendor list used for a part whose brand has no vendors of its own.
    default_brand: str = "*"
    # How often the scheduler sends queued questions and moves past silent vendors.
    tick_minutes: int = max(1, _int("ADVANCE_ORDER_TICK_MINUTES", 5))


advance_order_settings = AdvanceOrderSettings()
