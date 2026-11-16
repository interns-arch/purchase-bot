"""Check: the console's Groups page API (29 Oct 2026). Whapi is faked.

    .venv\\Scripts\\python.exe -m backend.scripts.check_groups_page
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="groups-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "WHAPI_ENABLED": "true", "WHAPI_TOKEN": "t", "WHAPI_SHARED_NUMBER": "true",
    "WHAPI_OWNER_NUMBER": "919217030408",
    "WHAPI_OWNER_NAME": "Alam sir",
    "WHAPI_TEAM_GROUP": "120363000000000001@g.us",
    "WHAPI_VENDOR_GROUPS": "CT_ND=120363000000000002@g.us",
    "STOCK_FORMAT_TEAM_NUMBERS": "Seema:9289015770,Alam:9217030408,Rajkumar:9217396458",
    "VENDOR_PURCHASE_TEAM_NUMBERS": "Seema:9289015770,Rajkumar:9217396458",
    "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1",
})
TEAM, VENDOR, PRIVATE = "120363000000000001@g.us", "120363000000000002@g.us", "120363999999999999@g.us"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


class R:
    def __init__(self, body=None, content=b""):
        self._b, self.content = body or {}, content

    def json(self):
        return self._b


HISTORY = {
    TEAM: [
        {"id": "b", "type": "text", "from": "919217030408", "from_me": True, "source": "api", "timestamp": 1793274905, "text": {"body": "Stock update kar diya ✅"}},
        {"id": "a", "type": "document", "from": "919289015770", "from_name": "Seema", "from_me": False, "source": "mobile", "timestamp": 1793274900,
         "document": {"id": "med-1", "file_name": "ford.xls", "caption": "Ark Ford"}},
        {"id": "c", "type": "text", "from": "919217030408", "from_me": True, "source": "mobile", "timestamp": 1793274910, "text": {"body": "ok"}},
        {"id": "d", "type": "action", "from": "919217030408", "timestamp": 1793274911},
    ],
    VENDOR: [],
}


def fake_request(method, path, **kwargs):
    if path.startswith("/messages/list/"):
        gid = path.rsplit("/", 1)[1]
        msgs = list(HISTORY.get(gid, []))
        return R({"messages": msgs[: kwargs.get("params", {}).get("count", 100)] if kwargs.get("params", {}).get("count") == 1 else msgs})
    if path == "/groups":
        return R({"groups": [{"id": TEAM, "name": "CarTrends Purchase Bot"}, {"id": VENDOR, "name": "CarTrends 🤝 Northend Distributors"}]})
    if path.startswith("/groups/"):
        return R({"name": {TEAM: "CarTrends Purchase Bot", VENDOR: "CarTrends 🤝 Northend Distributors"}.get(path.split("/")[2], "?")})
    if path.startswith("/media/"):
        return R(content=b"file-bytes")
    raise AssertionError(path)


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.db import init_db
    import backend.app.integrations.whatsapp.models  # noqa: F401 -- tables exist before init_db
    from backend.app.api.routes import groups as route
    from backend.app.auth.dependencies import get_current_user
    from backend.app.integrations.whatsapp import whapi

    init_db(force=True)
    whapi._request = fake_request
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    tc = TestClient(app)

    print("\n[1] list")
    groups = tc.get("/api/groups").json()
    check("only the bot's 2 groups", sorted(g["id"] for g in groups) == sorted([TEAM, VENDOR]))
    team = next(g for g in groups if g["id"] == TEAM)
    check("team group named and tagged", team["name"] == "CarTrends Purchase Bot" and team["role"] == "team")
    check("vendor group tagged with its code", next(g for g in groups if g["id"] == VENDOR)["role"] == "vendor CT_ND")

    print("\n[2] one group")
    body = tc.get(f"/api/groups/{TEAM}").json()
    msgs = body["messages"]
    check("oldest first, system actions left out", [m["id"] for m in msgs] == ["a", "b", "c"])
    check("Seema's file with its name and caption", msgs[0]["filename"] == "ford.xls" and msgs[0]["text"] == "Ark Ford" and msgs[0]["by"] == "team")
    check("the bot's post is marked as the bot", msgs[1]["by"] == "bot")
    check("Alam sir typing on his phone is Alam sir", msgs[2]["by"] == "owner" and msgs[2]["name"] == "Alam sir")
    check("time in IST", msgs[0]["created_at"].endswith("+05:30"))

    print("\n[3] the live feed")
    whapi.record(TEAM, "received file", "919289015770", "ford.xls")
    whapi.record(TEAM, "posted", None, "Stock update kar diya ✅ (120 items)")
    whapi.record("", "stock request", None, "Morning stock request: asked in 34 vendor groups")
    feed = tc.get("/api/groups/activity").json()
    check("newest first, with the group's name", [f["kind"] for f in feed] == ["stock request", "posted", "received file"] and feed[2]["group"].startswith("Team"))
    check("who took the file is named", feed[2]["who"] == "Seema ji")
    check("a run across all groups is labelled so", feed[0]["group"] == "All vendor groups")
    newer = tc.get("/api/groups/activity", params={"after": feed[1]["id"]}).json()
    check("'after' returns only the newer rows", [f["id"] for f in newer] == [feed[0]["id"]])

    print("\n[4] privacy")
    check("Alam sir's other groups cannot be opened", tc.get(f"/api/groups/{PRIVATE}").status_code == 404)
    check("a group file downloads", tc.get("/api/groups/media/med-1").content == b"file-bytes")

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
