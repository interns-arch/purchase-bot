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

    # --- quote comparison --------------------------------------------------
    # How many vendors are asked about one brand AT THE SAME TIME. 1 restores
    # the original first-vendor-wins behaviour exactly. Above 1, the answers
    # are collected and ranked, which is the only way a discount can be
    # compared -- you cannot compare what you never asked for.
    #
    # Founder, 1 Oct 2026: ask ONE vendor at a time, brand-wise, and the next
    # only after a refusal -- so the default is 1. With 1 the first vendor who
    # says yes wins, and vendors are asked in their brand priority order
    # (best known discount first, from VENDOR BRAND MAPPING.xlsx).
    quote_fanout: int = max(1, _int("ADVANCE_ORDER_QUOTE_FANOUT", 1))
    # How long the answers are collected before the best one is taken. A line
    # is decided as soon as every asked vendor has answered, so this is the
    # cap, not the wait. Kept below vendor_wait_minutes by default so a
    # customer is never left waiting on a silent vendor.
    quote_window_minutes: int = max(5, _int("ADVANCE_ORDER_QUOTE_WINDOW_MINUTES", 60))

    # TAT BANDS, in days, as upper bounds: "1,3,7,15" means same/next day,
    # 2-3 days, 4-7, 8-15, then everything slower. The Founder's rule is that
    # a better TAT beats a better discount -- but only across a band, so
    # "one day sooner, nine percent worse" never wins. Two vendors inside one
    # band are equal on time and the higher discount decides.
    tat_bands: list[int] = [
        int(x) for x in (os.environ.get("ADVANCE_ORDER_TAT_BANDS") or "1,3,7,15").split(",") if x.strip().isdigit()
    ] or [1, 3, 7, 15]

    # A RATE vendor has no standing percentage, so he is asked for a rate --
    # in ONE message listing every part, with the reply shape spelled out
    # ("TT-100 rate 450, 3 din"). A rate written beside a part number is read;
    # a loose rate while several parts were asked is refused as ambiguous and
    # goes to the admin (see parser.parse_vendor_reply). One message per part
    # was tried and rejected: WhatsApp does not say which message a reply
    # answers, and the first reply closed the question, losing the second.

    # A vendor who has only PART of a line ("sirf 3" of 10): the line becomes
    # his 3, and a new line for the other 7 goes on to the next vendor. The
    # total asked for never changes. false = his 3 is the answer, as before.
    split_partial: bool = (os.environ.get("ADVANCE_ORDER_SPLIT_PARTIAL", "true").strip().lower() == "true")

    # Who is told when NO vendor has a part (Founder, 1 Oct 2026: "send the
    # query to a human person's WhatsApp"). Comma-separated. Blank = the admin
    # numbers and purchase team, until the Founder names the person.
    human_numbers: list[str] = [
        part.strip() for part in (os.environ.get("ADVANCE_ORDER_HUMAN_NUMBERS") or "").split(",") if part.strip()
    ]

    # Case 2: how long a dealer-stock order may take, end to end, before what
    # is still open is reported to a person (Founder: 12 hours).
    dealer_stock_window_hours: int = max(1, _int("ADVANCE_ORDER_DEALER_STOCK_WINDOW_HOURS", 12))

    # --- handing the answer back to the sales bot --------------------------
    # Blank = push disabled and the sales bot polls GET /api/advance-orders/{id},
    # which keeps working either way. Set it and ProcureHub POSTs the order
    # (same JSON the GET returns) when a line is first quoted and again when
    # the order settles. Best-effort: a callback failure is logged and retried,
    # and can never hold up a vendor conversation.
    callback_url: str = (os.environ.get("ADVANCE_ORDER_CALLBACK_URL") or "").strip()
    # Sent as X-Api-Key, and as an X-Signature HMAC of the body when set.
    callback_secret: str = (os.environ.get("ADVANCE_ORDER_CALLBACK_SECRET") or "").strip()
    callback_timeout_seconds: float = float(os.environ.get("ADVANCE_ORDER_CALLBACK_TIMEOUT_SECONDS") or 20)
    callback_max_attempts: int = max(1, _int("ADVANCE_ORDER_CALLBACK_MAX_ATTEMPTS", 4))
    # SHADOW: compute and log exactly what would be POSTed, send nothing.
    # Mirrors AI_SHADOW_MODE and DEALER_PORTAL_SHADOW. Default false, because
    # the callback is inert anyway until callback_url is set.
    callback_shadow: bool = (
        os.environ.get("ADVANCE_ORDER_CALLBACK_SHADOW", "false").strip().lower() == "true"
    )


advance_order_settings = AdvanceOrderSettings()
