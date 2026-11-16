"""Check: Vendors page POC tab (14 Nov 2026). A POC number counts as the
vendor one to one and in his group; team numbers and another vendor's
numbers are refused. Throwaway SQLite, Whapi faked.

    .venv\\Scripts\\python.exe -m backend.scripts.check_vendor_pocs
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="pocs-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "WHAPI_ENABLED": "true", "WHAPI_TOKEN": "t", "WHAPI_SHARED_NUMBER": "false",
    "WHAPI_OWNER_NUMBER": "919217030408",
    "WHAPI_TEAM_GROUP": "120363000000000001@g.us",
    "WHAPI_VENDOR_GROUPS": "CT_AA=120363000000000002@g.us",
    "WHAPI_ACTIVE_GROUPS": "",
    "WHAPI_VENDOR_GROUP_MODE": "same",
    "STOCK_FORMAT_TEAM_NUMBERS": "Seema:9289015770,Alam:9217030408,Rajkumar:9217396458",
    "VENDOR_PURCHASE_TEAM_NUMBERS": "Seema:9289015770,Rajkumar:9217396458",
    "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1",
})
GROUP = "120363000000000002@g.us"
failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def msg(chat, sender, text=None, doc=None):
    m = {"id": f"m{sender}{text or doc}", "chat_id": chat, "from": sender, "from_me": False, "source": "mobile",
         "timestamp": 1793274900}
    if doc:
        m.update(type="document", document={"id": f"d-{doc}", "file_name": doc})
    else:
        m.update(type="text", text={"body": text})
    return m


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.api.routes import vendor_pocs as route
    from backend.app.auth.dependencies import get_current_user
    from backend.app.integrations.whatsapp import registry, whapi
    from core.db import get_session, init_db
    from core.models import Vendor

    init_db(force=True)
    with get_session() as s:
        ark = Vendor(name="Ark Automotive", vendor_code="CT_AA")
        other = Vendor(name="Other Spares", vendor_code="CT_OS")
        s.add_all([ark, other])
        s.flush()
        registry.register_vendor_number("9999922732", ark.id, s)
        registry.register_vendor_number("9811111111", other.id, s)
        ark_id, other_id = ark.id, other.id

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    tc = TestClient(app)

    print("\n[1] list and add")
    vendors = tc.get("/api/vendor-pocs/vendors").json()
    check("vendors listed with their registered count",
          any(v["vendor_id"] == ark_id and v["registered"] == 1 and v["pocs"] == 0 for v in vendors))
    r = tc.post(f"/api/vendor-pocs/{ark_id}", json={"number": "98765 43210", "name": "Ramesh"})
    body = r.json()
    check("a POC is added (10 digits -> +91)", r.status_code == 201
          and any(n["number"] == "919876543210" and n["kind"] == "poc" and n["name"] == "Ramesh" for n in body["numbers"]))
    check("the registered number is shown too", any(n["kind"] == "registered" for n in body["numbers"]))
    r2 = tc.post(f"/api/vendor-pocs/{ark_id}", json={"number": "9876500000"})
    check("several POCs per vendor", r2.status_code == 201 and sum(n["kind"] == "poc" for n in r2.json()["numbers"]) == 2)

    print("\n[2] refused")
    check("our team's number is refused", tc.post(f"/api/vendor-pocs/{ark_id}", json={"number": "9289015770"}).status_code == 400)
    check("a number registered to another vendor is refused",
          tc.post(f"/api/vendor-pocs/{ark_id}", json={"number": "9811111111"}).status_code == 409)
    check("another vendor's POC is refused",
          tc.post(f"/api/vendor-pocs/{other_id}", json={"number": "9876543210"}).status_code == 409)
    check("a bad number is refused", tc.post(f"/api/vendor-pocs/{ark_id}", json={"number": "12345x"}).status_code in (400, 422))

    print("\n[3] a POC IS the vendor -- one to one and in his group")
    with get_session() as s:
        party = registry.lookup("919876543210", s)
    check("one to one: the POC number is Ark", party is not None and party.party_type == "vendor" and party.party_id == ark_id)
    whapi.forget_cached_groups()
    texts, files = whapi.parse({"messages": [
        msg(GROUP, "919876543210", doc="ramesh stock.xlsx"),
        msg(GROUP, "919000000009", doc="stranger.xlsx"),
    ]})
    check("in the group: the POC's file is taken as Ark's, a stranger's is not",
          [f.filename for f in files] == ["ramesh stock.xlsx"])

    print("\n[4] remove")
    poc_id = next(n["id"] for n in body["numbers"] if n["number"] == "919876543210")
    after = tc.delete(f"/api/vendor-pocs/{ark_id}/{poc_id}").json()
    check("removed from the list", not any(n["number"] == "919876543210" for n in after["numbers"]))
    with get_session() as s:
        check("one to one: no longer the vendor", registry.lookup("919876543210", s) is None)
    whapi.forget_cached_groups()
    _t, files = whapi.parse({"messages": [msg(GROUP, "919876543210", doc="late.xlsx")]})
    check("in the group: no longer taken", not files)
    check("the registered number cannot be removed here", not any(n["kind"] == "registered" and n["removable"] for n in after["numbers"]))
    check("removed POC can now go to another vendor",
          tc.post(f"/api/vendor-pocs/{other_id}", json={"number": "9876543210"}).status_code == 201)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
