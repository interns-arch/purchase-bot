"""Check: the vendor form's brand and discount questions (21 Nov 2026).

  * brands are checked against the Dealer Portal's brand list; a misspelt or
    unknown brand is shown with the Portal's name and saved only after OK (or
    a corrected reply);
  * the discount is asked right after, naming the brands, as plain numbers in
    the same order ("12" / "12, 10"); "Ford - 8" still works;
  * changing the brands drops the old discounts; no Portal list -> as typed.

Throwaway SQLite; the AI reader is off and the Portal brand list is faked.

    .venv\\Scripts\\python.exe backend/scripts/check_vendor_brand_questions.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vbq-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "VENDOR_ONBOARDING_ENABLED": "true", "AI_LLM_ENABLED": "false",
    "DEALER_PORTAL_ADMIN_USERNAME": "", "DEALER_PORTAL_ADMIN_PASSWORD": "", "DEALER_PORTAL_ACCOUNTS_FILE": "",
})
failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from core.db import get_session, init_db
    from backend.app.vendor_onboarding import ai_chat, conversation, portal_brands
    from backend.app.vendor_onboarding import models as m
    import backend.app.advance_orders.models  # noqa: F401

    init_db(force=True)
    ai_chat.available = lambda: False
    master = {"list": ["HARISONS", "MAHINDRA", "FORD", "MARUTI SUZUKI", "MARUTI SUZUKI ACCESSORIES", "BOSCH"]}
    portal_brands.cached_master = lambda: master["list"]
    number = "919100000777"

    def new_draft() -> None:
        with get_session() as s:
            for old in s.query(m.VendorRegistrationRequest).all():
                old.status = m.REG_CANCELLED
            s.add(m.VendorRegistrationRequest(
                whatsapp_number=number, current_step="brands_supplied", status=m.REG_DRAFT,
                answers={"gstin": "06AACCT1515E1ZV", "vendor_name": "TEST AUTO", "mobile": "9811111111"}))

    def say(text: str) -> str:
        with get_session() as s:
            return "\n".join(conversation.answer(number, text, s) or [])

    def answers() -> dict:
        with get_session() as s:
            r = conversation.open_draft(number, s)
            return dict(r.answers or {}), r.current_step

    print("\n[1] misspelt brand -> the Portal's name is shown, saved after OK")
    new_draft()
    reply = say("harison")
    a, step = answers()
    check("'harison' -> shows HARISONS and asks OK", "HARISONS" in reply and "OK" in reply)
    check("...nothing saved yet, still on the brand question", "brands_supplied" not in a and step == "brands_supplied")
    reply = say("ok")
    a, step = answers()
    check("OK -> saved as HARISONS", a.get("brands_supplied") == "HARISONS" and "_brand_check" not in a)
    check("...next: the discount, naming HARISONS, as a number", step == "brand_discounts" and "HARISONS" in reply
          and "number" in reply.lower())
    say("40.32")
    a, step = answers()
    check("'40.32' -> HARISONS 40.32%", a.get("brand_discounts") == [{"brand": "HARISONS", "discount_pct": "40.32"}]
          and step == "account_holder")

    print("\n[2] typo, then he types it right himself")
    new_draft()
    say("mahindr")
    say("mahindra")
    a, _ = answers()
    check("'mahindr' then 'mahindra' -> MAHINDRA", a.get("brands_supplied") == "MAHINDRA")

    print("\n[3] exact names, two brands, discounts in order")
    new_draft()
    reply = say("Mahindra, Ford")
    a, step = answers()
    check("exact names saved straight away", a.get("brands_supplied") == "MAHINDRA, FORD" and step == "brand_discounts")
    check("...the discount question lists both", "MAHINDRA, FORD" in reply)
    reply = say("12")
    check("one number for two brands -> asks for both", "2 brands" in reply)
    say("12, 10")
    a, _ = answers()
    check("'12, 10' -> MAHINDRA 12, FORD 10", a.get("brand_discounts") == [
        {"brand": "MAHINDRA", "discount_pct": "12"}, {"brand": "FORD", "discount_pct": "10"}])

    print("\n[4] alias and unknown brand")
    new_draft()
    reply = say("maruti, xyz motors")
    check("'maruti' -> MARUTI SUZUKI shown, 'xyz motors' not found", "MARUTI SUZUKI" in reply and "nahi mila" in reply)
    say("ok")
    a, _ = answers()
    check("OK keeps the unknown one as typed", a.get("brands_supplied") == "MARUTI SUZUKI, XYZ MOTORS")

    print("\n[5] older discount format, SKIP")
    new_draft()
    say("ford")
    say("Ford - 8")
    a, _ = answers()
    check("'Ford - 8' still works", a.get("brand_discounts") == [{"brand": "FORD", "discount_pct": "8"}])
    new_draft()
    say("ford")
    say("skip")
    a, step = answers()
    check("SKIP -> no discount, form goes on", a.get("brand_discounts") is None and "brand_discounts" in a
          and step == "account_holder")

    print("\n[6] Portal list unreadable -> taken as typed")
    master["list"] = []
    new_draft()
    say("harison")
    a, _ = answers()
    check("no Portal list -> 'harison' saved as typed", a.get("brands_supplied") == "harison")
    master["list"] = ["HARISONS", "MAHINDRA", "FORD"]

    print("\n[7] changing the brands later drops the old discounts")
    with get_session() as s:
        r = conversation.open_draft(number, s)
        conversation._store(r, "brands_supplied", "FORD")
        conversation._store(r, "brand_discounts", [{"brand": "FORD", "discount_pct": "8"}])
        conversation._store(r, "brands_supplied", "MAHINDRA")
        check("old FORD discount removed", "brand_discounts" not in (r.answers or {}))

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
