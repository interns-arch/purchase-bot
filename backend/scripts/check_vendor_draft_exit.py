"""Check: an empty vendor registration never traps anyone (30 Oct 2026 --
Yash got "GSTIN bhejiye" to "stock upload" for two days).

    .venv\\Scripts\\python.exe -m backend.scripts.check_vendor_draft_exit
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="draft-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({"WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1"})

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from core.db import get_session, init_db
    from core.time_utils import now_ist_naive
    from backend.app.vendor_onboarding import conversation
    from backend.app.vendor_onboarding import models as m

    init_db(force=True)
    conversation._answer_with_ai = lambda *a, **k: None  # no AI in a check
    YASH, SEEMA, OLD = "918800556388", "919289015770", "919000000001"
    now = now_ist_naive()
    with get_session() as s:
        s.add(m.VendorRegistrationRequest(whatsapp_number=YASH, answers={"_on_behalf": True}, created_at=now, updated_at=now))
        s.add(m.VendorRegistrationRequest(whatsapp_number=SEEMA, answers={"_on_behalf": True, "gstin": "07AAAAA0000A1Z5"}, created_at=now, updated_at=now))
        old = now - timedelta(hours=3)
        s.add(m.VendorRegistrationRequest(whatsapp_number=OLD, answers={"_on_behalf": True}, created_at=old, updated_at=old))

    print("\n[1] 'stock upload' with an empty form")
    with get_session() as s:
        replies = conversation.answer(YASH, "stock upload", s)
    check("not answered by the form (goes to normal stock handling)", replies is None)
    with get_session() as s:
        check("the empty form is closed", conversation.open_draft(YASH, s) is None)

    print("\n[2] 'stock upload' with a half-filled form")
    with get_session() as s:
        replies = conversation.answer(SEEMA, "mujhe stock upload karna h", s)
    check("told how to leave it, nothing lost", replies and "cancel" in replies[0])
    with get_session() as s:
        check("the half-filled form stays open", conversation.open_draft(SEEMA, s) is not None)

    print("\n[3] an empty form left for 3 hours")
    with get_session() as s:
        check("expired (2 hours for an empty form)", conversation.open_draft(OLD, s) is None)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
