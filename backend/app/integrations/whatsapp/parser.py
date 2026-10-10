"""Parses Meta's webhook payload shape
(`entry[].changes[].value.messages[]`) into a flat list of document
attachments to process. WhatsApp messages carry at most one attachment
each -- "one WhatsApp message with multiple files" (per the business
requirement) is satisfied because Meta delivers each attachment as its own
`messages[]` entry; this parser naturally produces one
`IncomingWhatsAppMessage` per attachment, and the caller loops over all of
them independently so one bad attachment never blocks the others."""

from __future__ import annotations

from dataclasses import dataclass

_DOCUMENT_MESSAGE_TYPE = "document"
_IMAGE_MESSAGE_TYPE = "image"
_TEXT_MESSAGE_TYPE = "text"
_IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def is_for_this_number(value: dict) -> bool:
    """True when a webhook change was sent TO this bot's own WhatsApp number.

    The Meta business account holds FOUR numbers (+91 92170 30414 is this
    purchase bot; 30421, 30384 and 080 4447 5952 belong to the other bots)
    and Meta delivers every one of them to this webhook. Found live 6 Oct
    2026: a vendor answering the sales bot ("Available" + a photo) was read
    by the purchase bot, whose reply from 30414 then failed with 131047
    because that person had never written to 30414. Messages and delivery
    reports for the other numbers are ignored here."""
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    mine = (whatsapp_settings.phone_number_id or "").strip()
    target = str(((value or {}).get("metadata") or {}).get("phone_number_id") or "").strip()
    if not mine or not target:
        return True  # cannot tell -- keep the old behaviour
    if target != mine:
        senders = [m.get("from") for m in (value.get("messages") or [])]
        if senders:
            from core.logging_setup import get_logger

            get_logger(__name__).info(
                "WhatsApp webhook: ignoring %d message(s) from %s sent to another number on the "
                "account (%s, phone_number_id=%s).",
                len(senders), ", ".join(str(s) for s in senders),
                ((value.get("metadata") or {}).get("display_phone_number") or "?"), target,
            )
        return False
    return True


@dataclass
class IncomingWhatsAppMessage:
    sender: str
    message_id: str
    timestamp: str | None
    caption: str | None
    media_id: str
    filename: str
    mime_type: str | None
    # A PHOTO (WhatsApp "image" message) rather than a document. Only a
    # registered vendor's photo is read -- as a stock list, confirmed with him
    # before import. Everyone else's photos are ignored, as they always were.
    is_photo: bool = False


@dataclass
class IncomingWhatsAppText:
    """A plain text message -- used by the command-routing layer to decide
    which import workflow the sender's next file should run through."""

    sender: str
    message_id: str
    text: str


def parse_webhook_payload(payload: dict) -> list[IncomingWhatsAppMessage]:
    messages: list[IncomingWhatsAppMessage] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if not is_for_this_number(value):
                continue
            for raw_message in value.get("messages", []):
                if raw_message.get("type") == _IMAGE_MESSAGE_TYPE:
                    image = raw_message.get("image", {})
                    media_id = image.get("id")
                    sender = raw_message.get("from")
                    if media_id and sender:
                        mime = image.get("mime_type")
                        extension = _IMAGE_EXTENSIONS.get((mime or "").split(";")[0].strip(), "jpg")
                        messages.append(
                            IncomingWhatsAppMessage(
                                sender=sender,
                                message_id=raw_message.get("id", ""),
                                timestamp=raw_message.get("timestamp"),
                                caption=image.get("caption"),
                                media_id=media_id,
                                filename=f"photo_{media_id}.{extension}",
                                mime_type=mime,
                                is_photo=True,
                            )
                        )
                    continue
                if raw_message.get("type") != _DOCUMENT_MESSAGE_TYPE:
                    continue

                document = raw_message.get("document", {})
                media_id = document.get("id")
                sender = raw_message.get("from")
                if not media_id or not sender:
                    continue

                messages.append(
                    IncomingWhatsAppMessage(
                        sender=sender,
                        message_id=raw_message.get("id", ""),
                        timestamp=raw_message.get("timestamp"),
                        caption=document.get("caption"),
                        media_id=media_id,
                        filename=document.get("filename") or f"{media_id}",
                        mime_type=document.get("mime_type"),
                    )
                )

    return messages


def parse_text_messages(payload: dict) -> list[IncomingWhatsAppText]:
    """Parse plain text messages out of the same webhook payload. Kept
    separate from `parse_webhook_payload` (documents) so each caller loops
    over exactly what it handles; the route schedules both."""
    texts: list[IncomingWhatsAppText] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if not is_for_this_number(value):
                continue
            for raw_message in value.get("messages", []):
                if raw_message.get("type") == "contacts":
                    # A shared contact card ("send inquiries to this person"):
                    # handed on as text so the vendor-reply code can read the
                    # number in it.
                    cards = []
                    for card in raw_message.get("contacts") or []:
                        name = (card.get("name") or {}).get("formatted_name") or ""
                        phones = [p.get("wa_id") or p.get("phone") or "" for p in card.get("phones") or []]
                        cards.append(" ".join(x for x in [name, *phones] if x))
                    if raw_message.get("from") and cards:
                        texts.append(
                            IncomingWhatsAppText(
                                sender=raw_message["from"],
                                message_id=raw_message.get("id", ""),
                                text="[contact] " + " ; ".join(cards),
                            )
                        )
                    continue
                if raw_message.get("type") != _TEXT_MESSAGE_TYPE:
                    continue

                sender = raw_message.get("from")
                body = (raw_message.get("text") or {}).get("body")
                if not sender or body is None:
                    continue

                texts.append(
                    IncomingWhatsAppText(
                        sender=sender,
                        message_id=raw_message.get("id", ""),
                        text=body,
                    )
                )

    return texts



@dataclass
class DeliveryStatus:
    """WhatsApp's report on a message WE sent (entry[].changes[].value.statuses[]).
    `status` is sent / delivered / read / failed. A failed one carries the
    reason -- most often 131047: the person has not messaged in 24 hours, so
    only an approved template can reach them."""

    message_id: str
    recipient: str
    status: str
    error_code: int | None = None
    error_title: str | None = None


def parse_delivery_statuses(payload: dict) -> list[DeliveryStatus]:
    out: list[DeliveryStatus] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if not is_for_this_number(change.get("value") or {}):
                continue
            for raw in (change.get("value") or {}).get("statuses", []) or []:
                errors = raw.get("errors") or [{}]
                code = errors[0].get("code")
                out.append(
                    DeliveryStatus(
                        message_id=raw.get("id", ""),
                        recipient=raw.get("recipient_id", ""),
                        status=raw.get("status", ""),
                        error_code=int(code) if isinstance(code, (int, str)) and str(code).isdigit() else None,
                        error_title=errors[0].get("title") or errors[0].get("message"),
                    )
                )
    return out
