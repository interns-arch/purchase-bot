"""Gemini makes the bot talk like a person instead of a form.

Two jobs, both with the SAME rule as the staff assistant: the AI reads and
writes language, the code decides facts.

1. `understand(...)` reads whatever the vendor typed -- Hinglish, several
   details in one message, corrections, questions ("bank details kyun
   chahiye?") -- and returns which form fields it contains. Every value is
   then checked by `validators` (GSTIN checksum, GST portal, PAN/IFSC
   rules); the AI never decides that a value is valid.
2. `compose(...)` turns the outcome (what was saved, what was wrong, what to
   ask next) into a short, warm WhatsApp reply in the vendor's own language.

`route(...)` does the same for a message from an unknown number that is not
a command yet ("hello", "mujhe vendor banna hai", "ledger bhejna hai"):
it works out what they want and replies naturally, instead of the fixed menu.

Whenever the AI is unavailable or returns nothing usable, the caller falls
back to the fixed wording -- the bot never goes silent."""

from __future__ import annotations

import json
import threading

from backend.app.vendor_onboarding.questions import QUESTIONS
from core.logging_setup import get_logger

logger = get_logger(__name__)

PERSONA = (
    "You are the WhatsApp assistant of CarTrends Auto Parts (Delhi), helping vendors and "
    "customers of the purchase team. You are warm, polite and brief, like a helpful person on "
    "the purchase desk. Always reply in the SAME language and script the user writes in "
    "(Hindi, Hinglish in Roman script, or English). Use 'aap' politely in Hindi. WhatsApp style: "
    "short lines, at most one emoji, no markdown headings, no asterisks. Never claim to be a "
    "human if asked; say you are CarTrends' assistant. Never invent facts, approvals, prices "
    "or promises. Never say whether CarTrends buys, sells or stocks a particular brand or part, "
    "or quote any price, stock or delivery time — say the purchase team will confirm that, and "
    "offer to register them as a vendor or take their file."
)

FAQ = (
    "Facts you may use when the user asks:\n"
    "- Registration takes about 5 minutes on WhatsApp; after it, our approvers review it and the "
    "vendor gets a vendor code here.\n"
    "- GSTIN is needed for GST billing; we verify it on the GST portal and fill the company "
    "name and address from it.\n"
    "- Bank details are needed so that our accounts team can pay the vendor; they are kept with "
    "the accounts team only.\n"
    "- Brand-wise discount means the standing discount off MRP the vendor gives us per brand.\n"
    "- A ledger is the vendor's account statement for CarTrends; send it as a PDF, Excel or a "
    "clear photo with the caption LEDGER, and our accounts team checks it.\n"
    "- The user can type CANCEL to stop, and can correct any answer any time."
)


def available() -> bool:
    from backend.app.ai import llm

    return llm.available()


def _ask(system: str, user: str, *, learned: str | None = None, examples: bool = False) -> dict | None:
    """`learned`: which lessons to add ("understand"/"reply"/"route");
    `examples`: also add real past messages that were read correctly.
    This is how the bot improves day by day -- see `learning`."""
    from backend.app.ai import llm
    from backend.app.vendor_onboarding import learning

    if learned:
        system += learning.lessons_for_prompt(learned)
    if examples:
        system += learning.examples_for_prompt()
    try:
        data, _provider = llm.ask_json(system, user, purpose="chat")
    except Exception:  # noqa: BLE001 -- the caller falls back to fixed wording
        logger.exception("AI call failed.")
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ memory


_pending = threading.local()


def remember(number: str, role: str, text: str) -> None:
    """Queue a line of conversation memory. Written by `flush_memory()` AFTER
    the caller's own transaction has closed (memory uses its own session)."""
    if not hasattr(_pending, "rows"):
        _pending.rows = []
    _pending.rows.append((number, role, text))


def flush_memory() -> None:
    from backend.app.vendor_onboarding import learning

    learning.flush()  # the turn log the bot learns from, same timing
    rows, _pending.rows = getattr(_pending, "rows", []), []
    if not rows:
        return
    try:
        from backend.app.ai.staff_assistant import _remember

        for number, role, text in rows:
            _remember(number, role, text)
    except Exception:  # noqa: BLE001 -- memory is a nicety
        logger.exception("Could not store conversation memory.")


def history(number: str) -> str:
    try:
        from backend.app.ai.staff_assistant import _recent

        rows = [(role, said) for role, said, _ in _recent(number)]
    except Exception:  # noqa: BLE001
        rows = []
    rows += [(role, said) for n, role, said in getattr(_pending, "rows", []) if n == number]
    return "\n".join(f"{'User' if role == 'in' else 'Bot'}: {said[:400]}" for role, said in rows[-8:] if role in ("in", "out"))


def who_is(number: str) -> dict | None:
    """{"name": "Yash ji", "role": ...} for anyone the bot knows: the team
    lists in backend/.env first, then registered vendors and customers.
    None for a stranger."""
    from backend.app.vendor_onboarding.config import vendor_onboarding_settings as settings

    name = settings.honorific(number)
    if name:
        return {"name": name, "role": settings.role_of(number)}
    try:
        from backend.app.integrations.whatsapp import registry
        from core.db import get_session

        with get_session() as session:
            party = registry.lookup(number, session)
        if party is not None:
            return {"name": f"{party.name} ji", "role": f"registered CarTrends {party.party_type}"}
    except Exception:  # noqa: BLE001
        pass
    return None


def forget(number: str) -> None:
    try:
        from backend.app.ai.staff_assistant import _forget

        _forget(number)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ understanding


def _field_catalogue() -> str:
    lines = []
    for question in QUESTIONS:
        extra = ""
        if question.choices:
            extra = f" (choose one of: {', '.join(question.choices())})"
        if question.optional:
            extra += " [optional]"
        lines.append(f"- {question.key}: {question.label}{extra}")
    return "\n".join(lines)


def understand(
    message: str, *, current_key: str | None, answers: dict, chat_history: str, suggestions: dict | None = None
) -> dict | None:
    """{"fields": {key: value-or-null}, "intent": ..., "edit_field": key|None,
    "question": str|None} -- or None when the AI could not answer."""
    known = {k: v for k, v in (answers or {}).items() if not k.startswith("_") and v not in (None, "")}
    system = (
        "You extract vendor-registration details from a WhatsApp message for CarTrends. "
        "Reply with JSON only:\n"
        '{"fields": {"<field key>": "<value>"}, "intent": "answer|question|confirm|edit|cancel|smalltalk", '
        '"edit_field": "<field key or null>", "question": "<the user\'s question, or null>"}\n'
        "Fields:\n" + _field_catalogue() + "\n"
        "Rules:\n"
        "- Put a field in 'fields' ONLY if the user actually gave its value in THIS message. Copy "
        "values exactly (numbers, codes, names); do not invent or complete them.\n"
        "- A short reply with no field name answers the CURRENTLY ASKED field.\n"
        "- If the user declines an [optional] field ('nahi hai', 'no email', 'skip'), set it to null.\n"
        "- 'skip the rest' / 'baaki sab skip' / 'aur kuch nahi': set EVERY [optional] field that is not "
        "already collected to null.\n"
        "- For choice fields use the exact option text.\n"
        "- brand_discounts: one 'BRAND - PERCENT' per line, e.g. 'Maruti - 12\\nHyundai - 10'.\n"
        "- Agreeing with a suggested value ('ok', 'haan', 'sahi hai', 'yes', 'same', 'yahi', 'isi number pe', "
        "'this number', 'same as GST') for ANY field listed under 'Suggested values': set that field to "
        "\"__ACCEPT__\". E.g. 'number yahi hai' -> mobile: \"__ACCEPT__\" (that suggestion is their WhatsApp number).\n"
        "- intent 'confirm' = user approves the final summary ('confirm', 'sab sahi hai', 'submit kar do'); "
        "'edit' = wants to change a field without giving the new value (set edit_field); "
        "'cancel' = wants to stop registering; 'question' = asks something; 'smalltalk' = greeting/thanks."
    )
    user = (
        (f"Recent conversation:\n{chat_history}\n\n" if chat_history else "")
        + f"Already collected: {json.dumps(known, ensure_ascii=False, default=str)}\n"
        + "Suggested values (from the GST portal / their WhatsApp number): "
        + f"{json.dumps(suggestions or {}, ensure_ascii=False, default=str)}\n"
        + f"Currently asked field: {current_key or 'none (showing the final summary)'}\n"
        + f"User message: \"{message[:1500]}\""
    )
    data = _ask(system, user, learned="understand", examples=True)
    if not data:
        return None
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    valid_keys = {q.key for q in QUESTIONS}
    return {
        "fields": {k: v for k, v in fields.items() if k in valid_keys},
        "intent": str(data.get("intent") or "answer").strip().lower(),
        "edit_field": data.get("edit_field") if data.get("edit_field") in valid_keys else None,
        "question": (data.get("question") or None) if isinstance(data.get("question"), str) else None,
    }


# ------------------------------------------------------------------ replying


def compose(facts: dict, *, user_message: str, chat_history: str) -> str | None:
    """A natural reply built ONLY from `facts`. None -> use the fixed wording."""
    system = (
        PERSONA + "\n\n" + FAQ + "\n\n"
        "Write the bot's next WhatsApp message from the FACTS given. Reply with JSON only: "
        '{"reply": "<message>"}\n'
        "Rules:\n"
        "- Greet (Namaste / Hello) ONLY if the recent conversation is empty; otherwise go straight to the point.\n"
        "- Briefly acknowledge what was saved (do not repeat every value). Fields in 'skipped' were left "
        "blank on purpose — never call them saved.\n"
        "- If there are problems, explain each one simply and ask for it again.\n"
        "- If the user asked a question, answer it in one or two lines using the facts above only.\n"
        "- Then ask the next question naturally. If 'next_question' has 'choices', list them as a "
        "numbered list EXACTLY as given and say they can reply with the number. If it has a "
        "'suggestion' (ONE proposed value, never a numbered list), show it and ask them to reply OK if "
        "it is right, or type the correct one. "
        "If it is optional, say they can reply SKIP.\n"
        "- If 'show_summary' is true, do NOT write the summary yourself (it is appended after your "
        "message); just say please check the details below and reply CONFIRM, or tell what to change.\n"
        "- Mention ONLY things in FACTS. Ask ONLY 'next_question' (never ask for a ledger, documents or "
        "any other field), and never say a field was saved unless it is in 'saved'.\n"
        "- If FACTS has 'talking_to', that is who you are writing to: address them by name with 'ji', "
        "you are still the CarTrends assistant (never pretend to be them), and talk about the vendor "
        "in the third person (e.g. 'vendor ka GSTIN bhejiye').\n"
        "- Keep it under 70 words unless listing choices. Never say the vendor is approved."
    )
    user = (
        (f"Recent conversation:\n{chat_history}\n\n" if chat_history else "")
        + f"User's last message: \"{user_message[:800]}\"\n"
        + f"FACTS: {json.dumps(facts, ensure_ascii=False, default=str)}"
    )
    data = _ask(system, user, learned="reply")
    reply = (data or {}).get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    return reply.strip()


# ------------------------------------------------------------------ routing unknown messages

ROUTE_ACTIONS = {
    "register_vendor": "wants to register / become a new vendor or supplier of CarTrends",
    "ledger": "wants to send a ledger / account statement",
    "vendor_stock": "a vendor wants to send their stock list / inventory file",
    "customer_order": "wants to place an order / send a customer order file",
    "invoice": "wants to send an invoice",
    "chat": "greeting, question, or anything else",
}


def route(message: str, *, chat_history: str, onboarding_enabled: bool, person: dict | None = None) -> dict | None:
    """{"action": <ROUTE_ACTIONS key>, "reply": str} for a message from an
    unknown number, or None when the AI could not answer."""
    actions = {k: v for k, v in ROUTE_ACTIONS.items() if onboarding_enabled or k not in ("register_vendor", "ledger")}
    system = (
        PERSONA + "\n\n" + FAQ + "\n\n"
        "A message arrived from a number that is not yet a registered vendor or customer. Decide "
        "what they want and write the reply. Reply with JSON only: "
        '{"action": "<action>", "reply": "<message>"}\n'
        "Actions:\n" + "\n".join(f"- {k}: {v}" for k, v in actions.items()) + "\n"
        "What the bot can do (mention naturally when useful, not as a rigid menu):\n"
        "- register a new vendor (they just say so)\n"
        "- receive a vendor's stock file (Excel) — they send the file with the vendor name as caption\n"
        "- receive a customer order file (Excel)\n"
        "- receive an invoice (PDF)\n"
        + ("- receive a ledger (PDF/Excel/photo with caption LEDGER)\n" if onboarding_enabled else "")
        + "Rules for 'reply':\n"
        "- register_vendor: leave reply empty (the registration flow will greet them).\n"
        "- ledger / vendor_stock / customer_order / invoice: one or two friendly lines telling them "
        "to send the file now (for stock, add the vendor name as the caption).\n"
        "- chat: answer or greet briefly and say what you can help with. Under 60 words.\n"
        "- If 'Talking to' gives a name, use it exactly as written when greeting ('Hello Yash ji!', "
        "'Namaste Prateek sir!') and whenever they ask who they are; never say you don't know their name. "
        "If no name is given and they tell you their name, use it from then on."
    )
    who = (
        f"Talking to: {person['name']} ({person.get('role') or 'known contact'})\n"
        if person
        else "Talking to: unknown person (name not known yet)\n"
    )
    user = who + (f"Recent conversation:\n{chat_history}\n\n" if chat_history else "") + f"Message: \"{message[:800]}\""
    data = _ask(system, user, learned="route")
    if not data:
        return None
    action = str(data.get("action") or "chat").strip()
    if action not in actions:
        action = "chat"
    reply = data.get("reply") if isinstance(data.get("reply"), str) else ""
    return {"action": action, "reply": reply.strip()}
