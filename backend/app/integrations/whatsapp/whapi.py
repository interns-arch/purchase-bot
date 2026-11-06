"""Whapi.Cloud bridge: the purchase bot inside ordinary WhatsApp GROUPS
(27 Oct 2026).

Meta's Cloud API number (+91 92170 30414) cannot sit in a group, so a second,
ordinary WhatsApp number is linked to Whapi.Cloud by QR code (like WhatsApp
Web). Whapi posts every message that number sees to /api/whapi/webhook; this
module turns the group ones into the SAME `IncomingWhatsAppText` /
`IncomingWhatsAppMessage` the Meta webhook produces, so the whole bot (stock
files, commands, part questions, enquiry replies) runs unchanged. Replies
find their way back to the group through `route_for`, which `WhatsAppClient`
asks before every send.

Groups (env, read on every call):

  WHAPI_TEAM_GROUP      the purchase-team group: team alerts are posted here
                        instead of to each person; team members can send
                        stock files and ask the bot ("bot ...", a mention, or
                        a part number).
  WHAPI_VENDOR_GROUPS   "CT_MSP=1203...@g.us,CT_ND=1203...@g.us": a group per
                        vendor. Everything in it counts as FROM that vendor
                        (texts by our own team there are side talk and are
                        ignored), and everything the bot sends that vendor --
                        stock requests, reminders, part enquiries -- goes to
                        the group instead of his personal number.
  WHAPI_GROUPS          more groups that behave like the team group.

Every other chat on the linked phone -- above all the owner's PERSONAL chats
-- is ignored and never stored. Messages the bot itself sent through the API
come back with source "api" and are dropped, so it never answers itself.

SHARED NUMBER (29 Oct 2026): the linked phone is Alam sir's own WhatsApp.
With WHAPI_SHARED_NUMBER=true every bot post carries WHAPI_BOT_LABEL so the
group can tell the bot from him, an @mention of him is not a question for
the bot, and anything for him -- or for anyone in WHAPI_TEAM_MEMBERS -- is
posted in the team group (he cannot message himself). Replies are said the
way a person in the team would (`humanize`), a team-group text about stock,
parts or orders is answered without typing "bot", and system alerts never
go to a group (`direct_only`).

QUIET VENDOR GROUPS (WHAPI_VENDOR_GROUP_MODE=quiet): the bot takes only the
vendor's own stock FILES there, answers "Mil gaya ji" once, and reports the
result in the team group. It never chats with the vendor, ignores our
people's messages and files there, and his direct chats stay on Meta.

WHAPI_ENABLED=true, WHAPI_TOKEN and WHAPI_WEBHOOK_SECRET are required; the
token is never logged.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import os
import re
import threading
import time

import httpx

from core.logging_setup import get_logger

logger = get_logger(__name__)

PREFIX = "whapi:"
_REPLY_WINDOW_SECONDS = 60 * 60       # a group stays the reply route this long
_DUPLICATE_SECONDS = 10 * 60          # same text to the same group: send once
_VENDOR_CACHE_SECONDS = 5 * 60
_PART_NO = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b")
_TRIGGERS = ("bot ", "bot:", "bot,", "@bot", "/")
_RECEIVED = re.compile(r"^(✅|⚠️|ℹ️) .*\breceived\b", re.IGNORECASE)
# Approver replies ("APPROVE VR-12", "REJECT ADV-3 price high") are for the bot
# even though their codes are too short to look like part numbers.
_DECISION = re.compile(r"^(approve|approved|reject|rejected|pass|passed|fail|failed)\b", re.IGNORECASE)
# On a shared number nobody types "bot": a team-group text about stock, parts
# or orders is for the assistant (Hinglish as the team writes it). One that
# no tool answers gets no reply at all.
_WORK_WORDS = re.compile(
    r"\b(stock|stok|part|parts|kiske|kis ke|kiske paas|available|avl|availability|rate|price|order|"
    r"reminder|vendor|vendors|aaya|aya|aayi|bheja|bhejo|upload|portal|sheet)\b",
    re.IGNORECASE,
)
# Replies the bot sends everyone, said the way a person in the team would.
_HUMAN = [
    (re.compile(r"✅ Stock received successfully\. (\d+) item\(s\) imported\."), r"Stock update kar diya ✅ (\1 items)"),
    (re.compile(r"⚠️ Stock received\. (\d+) item\(s\) imported, (\d+) row\(s\) skipped\."),
     r"Stock update kar diya ✅ (\1 items). \2 lines mein part no./qty theek nahi thi, wo chhod di."),
    (re.compile(r"ℹ️ This stock file was already received earlier\. Nothing changed\."), "Ye file pehle hi aa chuki thi 👍"),
    (re.compile(r"ℹ️ '([^']+)' was already received a moment ago\. Nothing changed\."), r"'\1' abhi abhi aa chuki hai 👍"),
    (re.compile(r"⚠️ File received -- our team will review it\."), "File mil gayi, ek baar check karke batata hoon."),
    (re.compile(r"❌ We could not read this file\. Please send an Excel file containing Part Number and Quantity columns\."),
     "Ye file samajh nahi aa rahi. Excel mein Part No. aur Qty wali file bhej do please."),
    (re.compile(r"^Reason: ", re.MULTILINE), "Wajah: "),
    (re.compile(r"\s*\b[Pp]urchase [Bb]ot\b"), ""),
]

_lock = threading.Lock()
_routes: dict[str, tuple[str, float]] = {}           # number -> (group, last seen)
_recent: dict[tuple[str, str], tuple[str | None, float]] = {}
_vendor_cache: tuple[float, dict[str, str], dict[str, str], dict[str, str]] | None = None
# Inside `direct_only()` nothing goes to a group: system alerts (AI down,
# import errors) stay on the people's own chats.
_direct = contextvars.ContextVar("whapi_direct", default=False)


@contextlib.contextmanager
def direct_only():
    token = _direct.set(True)
    try:
        yield
    finally:
        _direct.reset(token)


def record(chat_id: str, kind: str, who: str | None, text: str | None) -> None:
    """One line in the Groups page's live feed. Never raises."""
    try:
        from backend.app.integrations.whatsapp.models import WhapiActivity
        from core.db import get_session
        from core.time_utils import now_ist_naive

        with get_session() as session:
            session.add(WhapiActivity(group_id=chat_id or "", kind=kind, who=who, text=(text or "")[:500], created_at=now_ist_naive()))
    except Exception:  # noqa: BLE001 -- the feed must never break the bot
        logger.exception("Whapi: could not record %s in %s.", kind, chat_id)


class WhapiError(Exception):
    """A Whapi.Cloud call failed."""


# ------------------------------------------------------------------ settings
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    return _env("WHAPI_ENABLED", "false").lower() == "true" and bool(_env("WHAPI_TOKEN"))


def _base_url() -> str:
    return _env("WHAPI_BASE_URL", "https://gate.whapi.cloud").rstrip("/")


def webhook_secret() -> str:
    return _env("WHAPI_WEBHOOK_SECRET")


def _group_id(raw: str) -> str:
    raw = raw.strip()
    return raw if not raw or "@" in raw else f"{raw}@g.us"


def team_group() -> str:
    return _group_id(_env("WHAPI_TEAM_GROUP"))


_db_groups_cache: tuple[float, dict[str, str]] | None = None
_DB_GROUPS_SECONDS = 60


def _db_vendor_groups() -> dict[str, str]:
    """{group id: vendor code} from `whapi_vendor_groups`. Cached a minute;
    {} when the table cannot be read (the env list still works)."""
    global _db_groups_cache
    now = time.monotonic()
    if _db_groups_cache and now - _db_groups_cache[0] < _DB_GROUPS_SECONDS:
        return _db_groups_cache[1]
    try:
        from sqlalchemy import select

        from backend.app.integrations.whatsapp.models import WhapiVendorGroup
        from core.db import get_session

        with get_session() as session:
            rows = {g: (c or "").upper() for g, c in session.execute(select(WhapiVendorGroup.group_id, WhapiVendorGroup.vendor_code))}
    except Exception:  # noqa: BLE001
        logger.exception("Whapi: could not read the vendor groups table.")
        rows = _db_groups_cache[1] if _db_groups_cache else {}
    _db_groups_cache = (now, rows)
    return rows


def forget_cached_groups() -> None:
    global _db_groups_cache, _vendor_cache
    _db_groups_cache = _vendor_cache = None


def vendor_group_codes() -> dict[str, str]:
    """{group id: vendor code (upper case)}: the vendor-groups table plus
    WHAPI_VENDOR_GROUPS. A vendor may have several groups:
    CT_EAA=1203...@g.us|1203...@g.us."""
    out: dict[str, str] = dict(_db_vendor_groups()) if enabled() else {}
    for item in _env("WHAPI_VENDOR_GROUPS").split(","):
        if "=" in item:
            code, groups = item.split("=", 1)
            for group in groups.split("|"):
                if code.strip() and group.strip():
                    out[_group_id(group)] = code.strip().upper()
    # WHAPI_ACTIVE_GROUPS: move group by group (Founder, 3 Nov 2026). When set,
    # only these vendor groups exist for the bot; every other one is ignored.
    active = {_group_id(g) for g in _env("WHAPI_ACTIVE_GROUPS").split(",") if g.strip()}
    return {g: c for g, c in out.items() if g in active} if active else out


def vendor_groups() -> dict[str, str]:
    """{vendor code: its first group id}."""
    out: dict[str, str] = {}
    for group, code in vendor_group_codes().items():
        out.setdefault(code, group)
    return out


def vendor_quiet() -> bool:
    """WHAPI_VENDOR_GROUP_MODE=quiet: in a vendor group the bot only takes the
    vendor's FILES, answers with one short "received", and tells the team
    group the rest. It never chats with the vendor there."""
    return _env("WHAPI_VENDOR_GROUP_MODE", "full").lower() in ("quiet", "same")


def vendor_same() -> bool:
    """WHAPI_VENDOR_GROUP_MODE=same (Founder, 3 Nov 2026): a vendor group works
    like the official number's chat. Also takes typed stock and photos from the
    vendor's own numbers, and the bot's replies go in the group, except anything
    about the Dealer Portal, which vendors are not told."""
    return _env("WHAPI_VENDOR_GROUP_MODE", "full").lower() == "same"


def _vendor_own_number(chat_id: str, number: str | None) -> bool:
    """Is `number` one of the numbers saved for this group's vendor?"""
    codes = vendor_group_codes()
    return bool(number) and codes.get(_vendor_maps()[0].get(number, "")) == codes.get(chat_id)


def shared_number() -> bool:
    return _env("WHAPI_SHARED_NUMBER", "false").lower() == "true"


def humanize(text: str) -> str:
    for pattern, said in _HUMAN:
        text = pattern.sub(said, text)
    return text.strip()


def _labelled(text: str | None) -> str:
    label = _env("WHAPI_BOT_LABEL", "🤖 *Purchase Bot:*") if shared_number() else ""
    if label.lower() in ("none", "off"):  # posts read as the owner himself
        label = ""
        text = humanize(text or "")
    return "\n".join(part for part in (label, text or "") if part)


def team_members() -> set[str]:
    """Numbers whose bot messages always go to the team group: WHAPI_TEAM_MEMBERS
    plus, on a shared number, its owner."""
    from backend.app.integrations.whatsapp.registry import normalize_number

    raw = _env("WHAPI_TEAM_MEMBERS").split(",") + ([_env("WHAPI_OWNER_NUMBER")] if shared_number() else [])
    return {n for n in (normalize_number(x.split(":")[-1].strip()) for x in raw if x.strip()) if n}


def allowed_groups() -> set[str]:
    extra = {_group_id(g) for g in _env("WHAPI_GROUPS").split(",") if g.strip()}
    return {g for g in {team_group(), *vendor_group_codes(), *extra} if g}


# ------------------------------------------------------------------ HTTP
def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_env('WHAPI_TOKEN')}"}


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    last: Exception | None = None
    for backoff in (0, 1, 3):
        if backoff:
            time.sleep(backoff)
        try:
            with httpx.Client(timeout=60) as client:
                response = client.request(method, f"{_base_url()}{path}", headers=_headers(), **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                last = WhapiError(f"Whapi {method} {path}: HTTP {response.status_code} {response.text[:200]}")
                continue
            if response.status_code >= 400:
                raise WhapiError(f"Whapi {method} {path}: HTTP {response.status_code} {response.text[:200]}")
            return response
        except httpx.HTTPError as exc:
            last = exc
    raise WhapiError(f"Whapi {method} {path} failed after retries: {last}")


def _sent_id(response: httpx.Response) -> str | None:
    try:
        return PREFIX + str((response.json().get("message") or {})["id"])
    except Exception:  # noqa: BLE001 -- a sent message stays sent
        return None


def _once(chat_id: str, key: str, send) -> str | None:
    """One post per group for the same content within 10 minutes: a vendor
    with three numbers mapped to one group must not see an enquiry three times."""
    digest = hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()
    now = time.monotonic()
    with _lock:
        for k in [k for k, (_, t) in _recent.items() if now - t > _DUPLICATE_SECONDS]:
            _recent.pop(k, None)
        if (chat_id, digest) in _recent:
            logger.info("Whapi: same message already posted to %s -- not repeated.", chat_id)
            return _recent[(chat_id, digest)][0]
    message_id = send()
    with _lock:
        _recent[(chat_id, digest)] = (message_id, now)
    return message_id


def _quiet_vendor_group(chat_id: str) -> bool:
    return vendor_quiet() and chat_id in vendor_group_codes()


def _vendor_label(chat_id: str) -> str:
    return _vendor_maps()[2].get(chat_id) or vendor_group_codes().get(chat_id, "Vendor")


def _acknowledge(chat_id: str) -> str | None:
    """The one short reply a quiet vendor group gets -- once per 10 minutes."""
    ack = _env("WHAPI_VENDOR_ACK", "Mil gaya ji, thank you 👍")
    return _once(chat_id, "ack", lambda: _post(chat_id, ack, "acknowledged"))


def _post(chat_id: str, body: str, kind: str = "posted") -> str | None:
    message_id = _sent_id(_request("POST", "/messages/text", json={"to": chat_id, "body": body}))
    record(chat_id, kind, None, body)
    return message_id


def post_as_owner(chat_id: str, body: str) -> str | None:
    """Post exactly `body` in a group, as the owner would type it -- no label,
    no quiet-group diversion. For the bot's own scheduled lines (the daily
    stock request), never for replies."""
    return _once(chat_id, body, lambda: _post(chat_id, body))


def send_text(chat_id: str, body: str) -> str | None:
    if _quiet_vendor_group(chat_id) and vendor_same():
        if "portal" in body.lower():
            logger.info("Whapi: a message about the Portal was kept out of vendor group %s.", chat_id)
            return None
        # A file that came in fine gets just "Mil gaya ji" (Founder, 3 Nov 2026);
        # only a problem or a question is spelled out.
        if _RECEIVED.match(body):
            return _acknowledge(chat_id)
        return _once(chat_id, body, lambda: _post(chat_id, _labelled(body)))
    if _quiet_vendor_group(chat_id):
        _acknowledge(chat_id)
        return send_text(team_group(), f"*{_vendor_label(chat_id)}* ki file:\n{body}") if team_group() else None
    return _once(chat_id, body, lambda: _post(chat_id, _labelled(body)))


def send_document(chat_id: str, content: bytes, filename: str, mime_type: str, caption: str | None = None) -> str | None:
    if _quiet_vendor_group(chat_id):
        if not team_group():
            return None
        chat_id, caption = team_group(), f"*{_vendor_label(chat_id)}*: {caption or filename}"
    key = f"doc:{filename}:{hashlib.sha1(content).hexdigest()}"
    media = f"data:{mime_type};name={filename};base64,{base64.b64encode(content).decode()}"
    payload = {"to": chat_id, "media": media, "filename": filename}
    if _labelled(caption):
        payload["caption"] = _labelled(caption)

    def _send() -> str | None:
        message_id = _sent_id(_request("POST", "/messages/document", json=payload))
        record(chat_id, "posted file", None, f"{filename}" + (f" — {caption}" if caption else ""))
        return message_id

    return _once(chat_id, key, _send)


def download(media_id: str) -> bytes:
    """Bytes of a received file; `media_id` with or without the whapi: prefix."""
    return _request("GET", f"/media/{media_id.removeprefix(PREFIX)}").content


# ------------------------------------------------------------------ routing
def _vendor_maps() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """({vendor number: group}, {group: the vendor's main number},
    {group: vendor name}), from the vendor's registered numbers and enquiry
    contacts. Cached 5 minutes."""
    global _vendor_cache
    by_code: dict[str, list[str]] = {}
    for group, code in vendor_group_codes().items():
        by_code.setdefault(code, []).append(group)
    if not by_code:
        return {}, {}, {}
    now = time.monotonic()
    if _vendor_cache and now - _vendor_cache[0] < _VENDOR_CACHE_SECONDS:
        return _vendor_cache[1], _vendor_cache[2], _vendor_cache[3]

    from sqlalchemy import select

    from backend.app.advance_orders.models import AdvanceVendorContact
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from backend.app.integrations.whatsapp.registry import normalize_number
    from core.db import get_session
    from core.models import Vendor

    number_group: dict[str, str] = {}
    group_main: dict[str, str] = {}
    group_name: dict[str, str] = {}
    with get_session() as session:
        for vendor in session.execute(select(Vendor).where(Vendor.vendor_code.isnot(None))).scalars():
            groups = by_code.get((vendor.vendor_code or "").upper())
            if not groups:
                continue
            for group in groups:
                group_name[group] = vendor.name
            registered = session.execute(
                select(WhatsAppRegisteredNumber.whatsapp_number).where(WhatsAppRegisteredNumber.vendor_id == vendor.id)
            ).scalars().all()
            contacts = session.execute(
                select(AdvanceVendorContact.whatsapp_number).where(
                    AdvanceVendorContact.vendor_id == vendor.id, AdvanceVendorContact.active.is_(True)
                )
            ).scalars().all()
            numbers = [n for n in (normalize_number(x) for x in [*registered, *contacts]) if n]
            for number in numbers:
                number_group.setdefault(number, groups[0])
            for group in groups if numbers else []:
                group_main.setdefault(group, numbers[0])
    _vendor_cache = (now, number_group, group_main, group_name)
    return number_group, group_main, group_name


def route_for(number: str | None) -> str | None:
    """The group a message to `number` should go to, or None for the normal
    Meta route: the vendor's group when he has one, else the group this
    person last wrote in (within an hour). Never raises."""
    if not number or not enabled() or _direct.get():
        return None
    try:
        from backend.app.integrations.whatsapp.registry import normalize_number

        number = normalize_number(number) or number
        if team_group() and number in team_members():
            return None if team_group_down() else team_group()
        # Quiet vendor groups are a reply route only right after the vendor
        # posted there (below); his direct chats stay on the Meta number.
        group = None if vendor_quiet() else _vendor_maps()[0].get(number)
        if group:
            return group
        with _lock:
            seen = _routes.get(number)
        if seen and time.monotonic() - seen[1] < _REPLY_WINDOW_SECONDS:
            return None if seen[0] == team_group() and team_group_down() else seen[0]
    except Exception:  # noqa: BLE001 -- fall back to the Meta number
        logger.exception("Whapi: could not work out the route for %s.", number)
    return None


_TEAM_DOWN_SECONDS = 10 * 60
_team_down_at = 0.0


def mark_team_group_down() -> None:
    """The team group refused a post (e.g. the Whapi trial limit): for the
    next 10 minutes its members are messaged directly instead."""
    global _team_down_at
    _team_down_at = time.monotonic()


def team_group_down() -> bool:
    return bool(_team_down_at) and time.monotonic() - _team_down_at < _TEAM_DOWN_SECONDS


def remember(number: str, chat_id: str) -> None:
    with _lock:
        _routes[number] = (chat_id, time.monotonic())


def post_team(body: str) -> bool:
    """Post a team alert in the team group. False when there is no team
    group (the caller then messages people one by one) or the post failed."""
    group = team_group()
    if not group or not enabled():
        return False
    try:
        send_text(group, body)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Whapi: could not post the team alert to %s.", group)
        return False


# ------------------------------------------------------------------ inbound
def _is_internal(number: str) -> bool:
    """Our own people: admins, the purchase team list, and the stock-format
    team (Seema, Alam, Rajkumar)."""
    try:
        from backend.app.integrations.whatsapp.recipients import internal_file_recipients
        from backend.app.integrations.whatsapp.stock_format import team_numbers

        return number in {*internal_file_recipients(), *team_numbers()}
    except Exception:  # noqa: BLE001
        return False


def _sender_number(msg: dict) -> str | None:
    from backend.app.integrations.whatsapp.registry import normalize_number

    for raw in (msg.get("phone"), msg.get("from"), _env("WHAPI_OWNER_NUMBER") if msg.get("from_me") else None):
        if raw and "@lid" not in str(raw):
            number = normalize_number(str(raw).split("@", 1)[0])
            if number:
                return number
    return None


def _addressed(text: str) -> str | None:
    """A team-group text the bot should answer, with the "bot" prefix
    removed -- or None for ordinary chat between people."""
    stripped = text.strip()
    lowered = stripped.lower()
    for trigger in _TRIGGERS:
        if lowered.startswith(trigger):
            return stripped[len(trigger):].strip() or None
    if _DECISION.match(stripped):
        return stripped
    owner = _env("WHAPI_OWNER_NUMBER")
    if owner and not shared_number() and f"@{owner[-10:]}" in stripped.replace(" ", ""):
        return re.sub(r"@\d+", "", stripped).strip() or None
    if _PART_NO.search(re.sub(r"@\d+", "", stripped).upper()):  # a mention is not a part number
        return stripped
    if shared_number() and _WORK_WORDS.search(stripped):
        return stripped
    return None


def _mid_conversation(number: str) -> bool:
    """This person is answering the bot right now -- a vendor registration
    form open, or a "haan / nahi" the assistant is waiting for -- so their
    plain replies ("shell", "Liqui Moly", "haan") are for the bot even though
    nothing marks them so (Seema, 30 Oct 2026: her brands were taken for
    chat between people and the form sat waiting)."""
    try:
        from backend.app.ai import staff_assistant
        from backend.app.vendor_onboarding import conversation
        from core.db import get_session

        with get_session() as session:
            if conversation.open_draft(number, session) is not None:
                return True
        return staff_assistant._pending_action(number) is not None
    except Exception:  # noqa: BLE001 -- unsure: treat as ordinary chat
        logger.exception("Whapi: could not tell whether %s is mid-conversation.", number)
        return False


def parse(payload: dict) -> tuple[list, list]:
    """(texts, files) from a Whapi webhook body, already mapped to the bot's
    own message types. Only allowed groups; never our own API sends."""
    from backend.app.integrations.whatsapp.parser import IncomingWhatsAppMessage, IncomingWhatsAppText

    texts: list = []
    files: list = []
    allowed = allowed_groups()
    group_main = _vendor_maps()[1]
    vendor_group_ids = set(vendor_group_codes())
    quiet = vendor_quiet()
    for msg in payload.get("messages") or []:
        chat_id = msg.get("chat_id") or ""
        if chat_id not in allowed or msg.get("source") == "api":
            continue
        sender = _sender_number(msg)
        in_vendor_group = chat_id in vendor_group_ids
        if in_vendor_group:
            team_member = msg.get("from_me") or (bool(sender) and _is_internal(sender))
            if team_member and (quiet or msg.get("type") == "text"):
                continue  # our people talking to the vendor (or sending him a PO) -- not for the bot
            if quiet and msg.get("type") != "document" and not (vendor_same() and _vendor_own_number(chat_id, sender)):
                continue  # quiet: only the vendor's stock FILES, never his chat or photos
            sender = group_main.get(chat_id)  # the group speaks as the vendor
        if not sender:
            logger.warning("Whapi: message %s in %s has no usable sender number -- skipped.", msg.get("id"), chat_id)
            continue
        if not in_vendor_group or quiet:
            remember(sender, chat_id)
        message_id = PREFIX + str(msg.get("id"))
        kind = msg.get("type")
        if kind == "text":
            body = ((msg.get("text") or {}).get("body") or "").strip()
            if not in_vendor_group:
                body = body if body and _mid_conversation(sender) else _addressed(body)
            if body:
                texts.append(IncomingWhatsAppText(sender=sender, message_id=message_id, text=body))
                record(chat_id, "received", sender, body)
        elif kind in ("document", "image"):
            media = msg.get(kind) or {}
            if not media.get("id"):
                continue
            record(chat_id, "received file", sender, media.get("file_name") or media.get("filename") or kind)
            files.append(IncomingWhatsAppMessage(
                sender=sender,
                message_id=message_id,
                timestamp=str(msg.get("timestamp") or ""),
                caption=media.get("caption"),
                media_id=PREFIX + str(media["id"]),
                filename=media.get("file_name") or media.get("filename") or f"{kind}_{msg.get('id')}",
                mime_type=media.get("mime_type"),
                is_photo=kind == "image",
            ))
    return texts, files


# ------------------------------------------------------------------ setup
def health() -> dict:
    return _request("GET", "/health").json()


def list_groups() -> list[dict]:
    return (_request("GET", "/groups", params={"count": 500}).json() or {}).get("groups") or []


def set_webhook(url: str) -> dict:
    """Point the channel's webhook at us with the shared secret header.
    PATCH /settings resets settings it is not given -- fine for a fresh channel."""
    body = {"webhooks": [{
        "url": url,
        "mode": "body",
        "events": [{"type": "messages", "method": "post"}],
        "headers": {"X-Whapi-Secret": webhook_secret()},
    }]}
    return _request("PATCH", "/settings", json=body).json()
