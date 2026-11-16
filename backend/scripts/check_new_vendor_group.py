"""Check: a NEW vendor gets its own WhatsApp group (12 Nov 2026) --
"<Vendor> x CarTrends", the vendor's number plus NK Jain sir, Rajkumar,
Seema and Prateek sir (Alam sir is the linked number, so he is in it as its
creator), saved as the vendor's group and counted as active even with
WHAPI_ACTIVE_GROUPS set. Whapi is faked -- nothing is created.

    .venv\\Scripts\\python.exe -m backend.scripts.check_new_vendor_group
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vgroup-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "WHAPI_ENABLED": "true", "WHAPI_TOKEN": "t", "WHAPI_AUTO_VENDOR_GROUP": "true",
    "WHAPI_OWNER_NUMBER": "919217030408",
    "WHAPI_ACTIVE_GROUPS": "120363417667516652@g.us",
    "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1",
})
os.environ.pop("WHAPI_NEW_VENDOR_GROUP_MEMBERS", None)
failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


class _R:
    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b


def main() -> int:
    from core.db import init_db
    from backend.app.integrations.whatsapp import vendor_group, whapi
    import backend.app.integrations.whatsapp.models  # noqa: F401

    init_db(force=True)
    calls: list = []
    posts: list = []

    def request(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if path == "/groups" and method == "POST":
            ids = [f"{n}@s.whatsapp.net" for n in kw["json"]["participants"]]
            return _R({"group_id": "120363999999999999@g.us", "participants": [{"id": i} for i in ids]})
        return _R({})

    whapi._request = request
    whapi.post_as_owner = lambda group, text: posts.append((group, text))
    whapi.record = lambda *a, **k: None

    print("\n[1] name")
    check("'<Vendor> x CarTrends'", vendor_group.group_name("OM SAI RAM  ENTERPRISES") == "OM SAI RAM ENTERPRISES x CarTrends")
    check("long name cut to WhatsApp's 100 characters, suffix kept",
          len(vendor_group.group_name("A" * 150)) == 100 and vendor_group.group_name("A" * 150).endswith(" x CarTrends"))

    print("\n[2] group created for a new vendor")
    group = vendor_group.create_for_vendor("CT_OSR", "OM SAI RAM ENTERPRISES", "9810541225")
    made = next((c for c in calls if c[1] == "/groups"), None)
    print("     subject:", made and made[2]["subject"])
    print("     members:", made and made[2]["participants"])
    check("group made", group == "120363999999999999@g.us")
    check("subject is 'OM SAI RAM ENTERPRISES x CarTrends'", made and made[2]["subject"] == "OM SAI RAM ENTERPRISES x CarTrends")
    expected = ["919810541225", "919810238966", "919217396458", "919289015770", "919999492550"]
    check("vendor + Jain sir, Rajkumar, Seema, Prateek sir", made and made[2]["participants"] == expected)
    check("Alam sir (the linked number) is not 'added' -- he is the creator", made and "919217030408" not in made[2]["participants"])
    check("greeting posted in the group, once", len(posts) == 1 and posts[0][0] == group
          and "CarTrends ka support engine banne ke liye bahut shukriya" in posts[0][1])
    print("     greeting:\n       " + posts[0][1].replace("\n", "\n       ") if posts else "")

    print("\n[3] the bot listens in it")
    whapi.forget_cached_groups()
    codes = whapi.vendor_group_codes()
    check("saved as CT_OSR's group and active despite WHAPI_ACTIVE_GROUPS", codes.get(group) == "CT_OSR")

    print("\n[4] never twice")
    calls.clear()
    again = vendor_group.create_for_vendor("CT_OSR", "OM SAI RAM ENTERPRISES", "9810541225")
    check("a vendor that has a group gets no second one", again is None and not any(c[1] == "/groups" for c in calls))
    check("and no second greeting", len(posts) == 1)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
