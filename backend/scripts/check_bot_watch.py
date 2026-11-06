"""Self-contained check of the bot-down alerts. Runs against a THROWAWAY SQLite file
and never messages anyone:

    .venv\\Scripts\\python.exe -m backend.scripts.check_bot_watch

Exit code 0 = every check passed."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="bot-watch-check-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["AI_FALLBACK_ENABLED"] = "false"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_ADMIN_PHONE_NUMBER"] = ""
os.environ["BOT_WATCH_STATE_FILE"] = str(_TMP / "state.json")

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    import backend.app.main  # noqa: F401 -- registers every table
    from backend.app import bot_status
    from backend.app.integrations.whatsapp.models import WhatsAppChatMessage as Chat
    from backend.scripts import bot_watch as w
    from core.db import get_session, init_db
    from core.time_utils import now_ist_naive

    init_db(force=True)
    start = datetime(2026, 11, 3, 10, 0)
    down = [{"key": w.DOWN, "text": w.DOWN_TEXT, "repeat": True}]

    def run(state, found, minute):
        """One check; a message counts as sent."""
        now = start + timedelta(minutes=minute)
        message = w.evaluate(state, found, now)
        if message:
            w.told(state, message, now)
        return message

    print("\n[1] down, then up")
    s = w.new_state()
    check("working: nothing to say", run(s, [], 0) is None)
    check("first failed check: quiet", run(s, down, 2) is None)
    check("second failed check: quiet", run(s, down, 4) is None)
    message = run(s, down, 6)
    check("third failed check: the alert", message == "Purchase Bot is not working: the bot is not responding")
    check("no repeat straight away", run(s, down, 8) is None)
    check("first good check: not yet", run(s, [], 10) is None)
    message = run(s, [], 12)
    check("second good check: the confirmation", bool(message) and message.startswith("Purchase Bot is working again"))
    check("it says how long", "6 minutes" in message)
    check("afterwards: quiet", run(s, [], 14) is None and not s["active"] and not s["alerted"])

    print("\n[2] noise")
    s = w.new_state()
    for minute in (0, 2):
        run(s, down, minute)
    check("a deploy-length blip says nothing", run(s, [], 4) is None and run(s, [], 6) is None and run(s, [], 8) is None)
    check("and leaves nothing behind", not s["active"] and s["streaks"] == {})
    s = w.new_state()
    for minute in (0, 2, 4):
        run(s, down, minute)
    run(s, [], 6)
    check("back down after one good check: no second alert", run(s, down, 8) is None)
    check("and no confirmation in between", s["alerted"] and s["active"])

    print("\n[3] reminders")
    s = w.new_state()
    for minute in (0, 2, 4):
        run(s, down, minute)
    check("not before 2 hours", run(s, down, 120) is None)
    message = run(s, down, 124)
    check("after 2 hours: still not working", bool(message) and message.startswith("Purchase Bot is still not working"))
    plan = [{"key": "plan_x", "text": "nobody received the purchase plan", "repeat": False}]
    s = w.new_state()
    for minute in (0, 2):
        run(s, plan, minute)
    check("a missed plan alerts once", "purchase plan" in run(s, plan, 4))
    check("and is not repeated", run(s, plan, 200) is None)
    both = plan + [{"key": "jobs", "text": "scheduled work is failing", "repeat": True}]
    for minute in (202, 204):
        run(s, both, minute)
    message = run(s, both, 206)
    check("a new problem alerts with everything wrong", "scheduled work" in message and "purchase plan" in message)

    print("\n[4] a send that fails is retried")
    s = w.new_state()
    for minute in (0, 2):
        w.evaluate(s, down, start + timedelta(minutes=minute))
    first = w.evaluate(s, down, start + timedelta(minutes=4))
    second = w.evaluate(s, down, start + timedelta(minutes=6))
    check("still due at the next check", first and first == second)
    w.save_state(s)
    check("state survives a restart", w.load_state()["active"] == s["active"])

    print("\n[5] telling people")
    calls = []
    real_template = w._send_template
    w.numbers = lambda: ["A", "B"]
    w.whatsapp_settings.access_token, w.whatsapp_settings.phone_number_id = "t", "p"
    w._send_template = lambda to, template, text: calls.append((to, template)) or template != "purchase_bot_alert"
    check("not approved yet: falls back to the old template", w.send_whatsapp("x") and calls == [
        ("A", "purchase_bot_alert"), ("A", w.FALLBACK_TEMPLATE), ("B", "purchase_bot_alert"), ("B", w.FALLBACK_TEMPLATE)])
    calls.clear()
    w._send_template = lambda to, template, text: calls.append((to, template)) or True
    check("approved: only the alert template", w.send_whatsapp("x") and {t for _, t in calls} == {"purchase_bot_alert"})
    w._send_template = lambda to, template, text: False
    w.send_email = lambda text: True
    check("WhatsApp refused but email went: counts as told", w.tell("x"))
    w.send_email = lambda text: False
    check("nothing went: not told", not w.tell("x"))
    w._send_template = real_template
    check("alert text has no new lines for the template", "\n" not in w._flat("a\n\n b   c"))

    print("\n[6] what the bot says about itself")

    def clear():
        with get_session() as session:
            session.query(Chat).delete()

    def add(status, error=None, text="x", minutes_ago=1):
        with get_session() as session:
            session.add(Chat(whatsapp_number="91", direction="out", kind="text", text=text, status=status,
                             error=error, created_at=now_ist_naive() - timedelta(minutes=minutes_ago)))

    def keys():
        return [p["key"] for p in bot_status.problems()]

    check("nothing wrong: empty", keys() == [])
    for _ in range(6):
        add("failed", "131047 Re-engagement message")
    add("read")
    check("closed windows while others get through: no alert", keys() == [])
    clear()
    for _ in range(4):
        add("failed", "131049 limit")
    check("a few failures alone: no alert", keys() == [])
    add("failed", "131049 limit")
    check("5 failures and nothing delivered: alert", keys() == ["whatsapp_sending"])
    clear()
    add("failed", "131042 Business eligibility payment issue")
    check("payment problem: alert at once", keys() == ["whatsapp_account"])
    add("read")
    check("a later good send clears it", keys() == [])
    clear()
    plan_text = "🛒 Purchase plan by vendor A"
    add("failed", "131047 x", text=plan_text, minutes_ago=3)
    add("failed", "131047 x", text=plan_text, minutes_ago=3)
    check("nobody got the plan: alert", [k[:5] for k in keys()] == ["plan_"])
    check("and it is not repeated", not bot_status.problems()[0]["repeat"])
    add("read", text=plan_text, minutes_ago=3)
    check("one person got it: no alert", keys() == [])
    clear()
    bot_status._last_beat -= 400
    check("scheduler silent 5 minutes: alert", keys() == ["scheduler"])
    bot_status.beat()
    check("beating again: clear", keys() == [])
    bot_status.job_failed("poll_gmail_inbox")
    check("a failing job names itself", "poll_gmail_inbox" in bot_status.problems()[0]["text"])
    bot_status.job_ok("poll_gmail_inbox")
    check("its next success clears it", keys() == [])

    print("\n[7] the watcher's own check, and the real endpoint")
    os.environ["BOT_WATCH_URL"] = "http://127.0.0.1:9/none"
    check("no answer means bot_down", [p["key"] for p in w.check()] == [w.DOWN])
    from fastapi.testclient import TestClient

    body = TestClient(backend.app.main.app).get("/api/bot-status").json()
    check("/api/bot-status answers", body == {"working": True, "problems": []})

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
