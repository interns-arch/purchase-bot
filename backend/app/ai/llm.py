"""One way to ask an AI model a question, with the same fallback the sales bot
(AutoFlow) uses: Gemini first, then the NVIDIA models already on this server,
then nothing -- and the caller keeps its fixed rules.

    from backend.app.ai import llm
    data = llm.ask_json(system, user, purpose="reply")   # dict, or None

THE MODEL DECIDES THE WORDING OR THE MEANING, NEVER THE FACTS
-------------------------------------------------------------
Exactly AutoFlow's rule. Every caller of this module validates what comes
back against data it already holds (the vendor's own words, the database),
and drops any number it cannot trace. A None answer is always safe: the
caller falls back to what it did before AI was added.

PROVIDERS
---------
  gemini   GEMINI_API_KEY. Models default to the sales bot's: gemini-3.5-flash-lite
           for chat-type work, gemini-3.5-flash for reading replies and photos.
           A SEPARATE key from the sales bot is recommended: one bot running
           out of prepaid credit (as on 30 Sep 2026) should not stop the other.
  nvidia   NVIDIA_API_KEY (already configured): meta/llama-3.1-8b-instruct.

Health is tracked per provider (`health`), and a provider that keeps failing
raises the admins' AI-down alarm. Never raises.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

from backend.app.core.config import settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

ENABLED = os.environ.get("AI_LLM_ENABLED", "true").strip().lower() == "true"
GEMINI_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
GEMINI_BASE = (os.environ.get("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
GEMINI_MODELS = {
    "chat": os.environ.get("GEMINI_CHAT_MODEL", "gemini-3.5-flash-lite").strip(),
    "reply": os.environ.get("GEMINI_REPLY_MODEL", "gemini-3.5-flash").strip(),
    "vision": os.environ.get("GEMINI_VISION_MODEL", "gemini-3.5-flash").strip(),
}
NVIDIA_TEXT_MODEL = (os.environ.get("AI_LLM_NVIDIA_MODEL") or "meta/llama-3.1-8b-instruct").strip()
TIMEOUT = float(os.environ.get("AI_LLM_TIMEOUT_SECONDS", "30"))
TEMPERATURE = 0.2  # the sales bot's setting: low creativity
ORDER = [p.strip() for p in (os.environ.get("AI_LLM_PROVIDERS") or "gemini,nvidia").split(",") if p.strip()]

# AI-down alarm: after this many failures in a row a provider counts as down.
DOWN_AFTER_FAILURES = max(1, int(os.environ.get("AI_DOWN_AFTER_FAILURES", "3")))
ALARM_REPEAT_MINUTES = max(10, int(os.environ.get("AI_DOWN_ALARM_REPEAT_MINUTES", "60")))
ALARM_ENABLED = os.environ.get("AI_DOWN_ALARM_ENABLED", "true").strip().lower() == "true"


# ------------------------------------------------------------------ health
@dataclass
class _Health:
    failures: int = 0
    last_ok: float | None = None
    last_error: str | None = None
    down_since: float | None = None
    last_alarm: float = 0.0


@dataclass
class _Board:
    providers: dict[str, _Health] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


health = _Board()


def _record(provider: str, ok: bool, error: str | None = None) -> None:
    with health.lock:
        h = health.providers.setdefault(provider, _Health())
        if ok:
            was_down = h.down_since is not None
            h.failures, h.last_ok, h.down_since = 0, time.time(), None
            if was_down:
                _alarm(f"✅ AI wapas chal raha hai ({provider}).", key=f"up-{provider}", force=True)
            return
        h.failures += 1
        h.last_error = error
        if h.failures >= DOWN_AFTER_FAILURES and h.down_since is None:
            h.down_since = time.time()
        if h.down_since is not None and time.time() - h.last_alarm >= ALARM_REPEAT_MINUTES * 60:
            h.last_alarm = time.time()
            others = [p for p in ORDER if p != provider and is_configured(p)]
            plan = f"Ab {others[0]} se chal raha hai." if others else "Purchase bot apne fixed rules par chal raha hai."
            _alarm(
                f"⚠️ AI ({provider}) jawab nahi de raha ({error or 'no reply'}). {plan}",
                key=f"down-{provider}",
            )


def _alarm(text: str, *, key: str, force: bool = False) -> None:
    """To the admin numbers, off the caller's thread. Never raises."""
    if not ALARM_ENABLED:
        return
    logger.warning("AI health: %s", text)

    def send() -> None:
        try:
            from backend.app.integrations.whatsapp.config import whatsapp_settings
            from backend.app.integrations.whatsapp.outbound import send_reply_safe

            for number in whatsapp_settings.admin_phone_numbers:
                send_reply_safe(number, text)
        except Exception:  # noqa: BLE001
            logger.exception("Could not send the AI health alarm.")

    threading.Thread(target=send, name=f"ai-alarm-{key}", daemon=True).start()


def status() -> dict:
    """For the desk / Integration Status: which providers work right now."""
    with health.lock:
        return {
            name: {
                "configured": is_configured(name),
                "failures_in_a_row": h.failures,
                "down": h.down_since is not None,
                "last_error": h.last_error,
            }
            for name, h in ((p, health.providers.get(p, _Health())) for p in ORDER)
        }


# ------------------------------------------------------------------ providers
def is_configured(provider: str) -> bool:
    if provider == "gemini":
        return bool(GEMINI_KEY)
    if provider == "nvidia":
        return bool(settings.nvidia_api_key or settings.ai_api_key)
    return False


def available() -> bool:
    return ENABLED and any(is_configured(p) for p in ORDER)


def _gemini(system: str, user: str, purpose: str, image: tuple[bytes, str] | None) -> str | None:
    import base64

    model = GEMINI_MODELS.get(purpose) or GEMINI_MODELS["chat"]
    parts: list[dict] = [{"text": user}]
    if image is not None:
        data, mime = image
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": TEMPERATURE, "responseMimeType": "application/json"},
    }
    response = httpx.post(
        f"{GEMINI_BASE}/models/{model}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    candidates = response.json().get("candidates") or []
    texts = [p.get("text", "") for p in ((candidates[0].get("content") or {}).get("parts") or [])] if candidates else []
    return "".join(texts).strip() or None


def _nvidia(system: str, user: str, purpose: str, image: tuple[bytes, str] | None) -> str | None:
    if image is not None:
        return None  # photos go through backend.app.ai.vision's own NVIDIA models
    key = settings.nvidia_api_key or settings.ai_api_key
    base = (settings.nvidia_base_url or "https://integrate.api.nvidia.com/v1").rstrip("/")
    response = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        json={
            "model": NVIDIA_TEXT_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": TEMPERATURE,
            "max_tokens": 1500,
        },
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return (response.json().get("choices") or [{}])[0].get("message", {}).get("content")


_CALLERS = {"gemini": _gemini, "nvidia": _nvidia}


def extract_json(text: str | None) -> dict | None:
    """A JSON object out of a model reply, even inside ``` fences or prose."""
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def ask_json(system: str, user: str, *, purpose: str = "chat", image: tuple[bytes, str] | None = None) -> tuple[dict | None, str | None]:
    """(parsed JSON object, provider that answered) -- or (None, None) when no
    provider could. Tries each configured provider in AI_LLM_PROVIDERS order."""
    if not ENABLED:
        return None, None
    for provider in ORDER:
        if not is_configured(provider):
            continue
        caller = _CALLERS.get(provider)
        if caller is None:
            continue
        try:
            text = caller(system, user, purpose, image)
        except Exception as exc:  # noqa: BLE001 -- a provider failure is a fallback, not a crash
            _record(provider, False, str(exc)[:120])
            continue
        if text is None and image is not None and provider == "nvidia":
            continue  # not a failure: NVIDIA photos are handled elsewhere
        data = extract_json(text)
        if data is None:
            _record(provider, False, "no JSON in the reply")
            continue
        _record(provider, True)
        return data, provider
    return None, None
