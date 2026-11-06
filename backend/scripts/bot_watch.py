"""The Purchase Bot's watcher (Founder, 3 Nov 2026): tells people when the bot stops
working, and again when it is working again. Runs as its own container, so it keeps
going when the backend does not:

    python -m backend.scripts.bot_watch

Every BOT_WATCH_INTERVAL_SECONDS it reads /api/bot-status. Not answering is the
problem "bot_down"; anything else comes from `backend.app.bot_status`. To stay quiet
through a deploy or a passing hiccup, a problem must last BOT_WATCH_FAIL_CHECKS
checks in a row before anyone hears about it, and it must be gone for
BOT_WATCH_CLEAR_CHECKS before it counts as fixed.

It talks to WhatsApp directly, not through the backend or the database, because
those may be what is broken. It cannot report the server itself being off: that
needs an outside uptime monitor."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from backend.app.integrations.whatsapp.config import whatsapp_settings
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

DOWN = "bot_down"
DOWN_TEXT = "the bot is not responding"
FALLBACK_TEMPLATE = "purchase_bot_update"  # marketing: WhatsApp may hold it back, so only a fallback


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _list(name: str) -> list[str]:
    return [item.strip() for item in _env(name).split(",") if item.strip()]


def numbers() -> list[str]:
    """BOT_WATCH_NUMBERS, or the bot's admins when it is not set."""
    return _list("BOT_WATCH_NUMBERS") or list(whatsapp_settings.admin_phone_numbers)


# ------------------------------------------------------------------ deciding what to say
def new_state() -> dict:
    return {"streaks": {}, "active": {}, "clear_checks": 0, "since": None, "alerted": False, "reminded_at": None}


def _minutes(since: str, now: datetime) -> int:
    return max(1, round((now - datetime.fromisoformat(since)).total_seconds() / 60))


def alert_text(active: dict) -> str:
    return "Purchase Bot is not working: " + "; ".join(item["text"] for item in active.values())


def evaluate(state: dict, found: list[dict], now: datetime) -> str | None:
    """Update `state` with this check's problems and return the message that is now
    due, if any. Nothing is marked as told here: `told()` does that once a message
    has actually gone, so a failed send is retried at the next check."""
    fail_checks = _int("BOT_WATCH_FAIL_CHECKS", 3)
    remind_after = _int("BOT_WATCH_REMIND_MINUTES", 120)
    current = {item["key"]: item for item in found}

    state["streaks"] = {key: state["streaks"].get(key, 0) + 1 for key in current}
    new_active = False
    for key, item in current.items():
        if state["streaks"][key] >= fail_checks and key not in state["active"]:
            state["active"][key] = item
            new_active = True
    # A problem that goes away before anyone was told is simply forgotten.
    state["active"] = {key: item for key, item in state["active"].items() if key in current or state["alerted"]}

    if state["active"] and not state["since"]:
        state["since"] = now.isoformat()

    if state["active"] and any(key in current for key in state["active"]):
        state["clear_checks"] = 0
    elif state["active"]:
        state["clear_checks"] += 1
        if state["clear_checks"] >= _int("BOT_WATCH_CLEAR_CHECKS", 2):
            if not state["alerted"]:
                state.update(active={}, since=None, clear_checks=0)
                return None
            return f"Purchase Bot is working again. It had a problem for about {_minutes(state['since'], now)} minutes."
        return None
    else:
        state.update(since=None, clear_checks=0, alerted=False, reminded_at=None)
        return None

    # Anything fixed while something else is still wrong drops off the list.
    still_wrong = {key: item for key, item in state["active"].items() if key in current}
    if new_active or not state["alerted"]:
        return alert_text(still_wrong)
    worth_repeating = {key: item for key, item in still_wrong.items() if item.get("repeat", True)}
    reminded = datetime.fromisoformat(state["reminded_at"]) if state["reminded_at"] else now
    if worth_repeating and (now - reminded).total_seconds() >= remind_after * 60:
        return f"Purchase Bot is still not working, for about {_minutes(state['since'], now)} minutes: " + "; ".join(
            item["text"] for item in worth_repeating.values()
        )
    return None


def told(state: dict, message: str, now: datetime) -> None:
    if message.startswith("Purchase Bot is working again"):
        state.update(active={}, since=None, clear_checks=0, alerted=False, reminded_at=None)
    else:
        state.update(alerted=True, reminded_at=now.isoformat())


# ------------------------------------------------------------------ checking
def check() -> list[dict]:
    url = _env("BOT_WATCH_URL", "http://backend:8000/api/bot-status")
    try:
        response = httpx.get(url, timeout=30)
        response.raise_for_status()
        return response.json()["problems"]
    except Exception as exc:  # noqa: BLE001 -- not answering IS the finding
        logger.warning("Bot watch: %s did not answer properly (%s).", url, type(exc).__name__)
        return [{"key": DOWN, "text": DOWN_TEXT, "repeat": True}]


# ------------------------------------------------------------------ telling people
def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:900]


def _send_template(to: str, template: str, text: str) -> bool:
    response = httpx.post(
        f"{whatsapp_settings.graph_api_base_url}/{whatsapp_settings.phone_number_id}/messages",
        headers={"Authorization": f"Bearer {whatsapp_settings.access_token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": whatsapp_settings.template_language},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": _flat(text)}]}],
            },
        },
        timeout=30,
    )
    if response.status_code != 200:
        logger.warning("Bot watch: template %s to %s refused (HTTP %s).", template, to, response.status_code)
    return response.status_code == 200


def send_whatsapp(text: str) -> bool:
    if not (whatsapp_settings.access_token and whatsapp_settings.phone_number_id):
        return False
    sent = False
    for to in numbers():
        for template in (_env("BOT_WATCH_TEMPLATE", "purchase_bot_alert"), FALLBACK_TEMPLATE):
            try:
                if _send_template(to, template, text):
                    sent = True
                    break
            except Exception as exc:  # noqa: BLE001 -- one failure must not stop the next person
                logger.warning("Bot watch: could not reach WhatsApp (%s).", type(exc).__name__)
    return sent


def send_email(text: str) -> bool:
    from backend.app.integrations.gmail import mailer

    subject = text.split(":")[0].split(".")[0]
    return any([mailer.send_email(to, subject, text) for to in _list("BOT_WATCH_EMAILS")])


def tell(text: str) -> bool:
    """Every channel, so WhatsApp being the fault cannot hide the alert."""
    logger.warning("Bot watch: %s", text)
    results = []
    for channel in (send_whatsapp, send_email):
        try:
            results.append(channel(text))
        except Exception:  # noqa: BLE001 -- the other channel must still run
            logger.exception("Bot watch: %s failed.", channel.__name__)
            results.append(False)
    return any(results)


# ------------------------------------------------------------------ remembering
def state_path() -> Path:
    return Path(_env("BOT_WATCH_STATE_FILE", "/app/database/bot_watch_state.json"))


def load_state() -> dict:
    """Kept on disk so restarting the watcher repeats no alert and loses no recovery."""
    try:
        return {**new_state(), **json.loads(state_path().read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return new_state()


def save_state(state: dict) -> None:
    try:
        state_path().write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        logger.exception("Bot watch: could not save its state.")


def run_once(state: dict) -> None:
    now = now_ist_naive()
    message = evaluate(state, check(), now)
    if message and tell(message):
        told(state, message, now)
    save_state(state)


def main() -> None:
    logger.info("Bot watch started: %d WhatsApp number(s), %d email(s).", len(numbers()), len(_list("BOT_WATCH_EMAILS")))
    state = load_state()
    while True:
        try:
            run_once(state)
        except Exception:  # noqa: BLE001 -- the watcher must outlive anything it watches
            logger.exception("Bot watch: this check failed.")
        time.sleep(_int("BOT_WATCH_INTERVAL_SECONDS", 120))


if __name__ == "__main__":
    main()
