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


@dataclass
class IncomingWhatsAppMessage:
    sender: str
    message_id: str
    timestamp: str | None
    caption: str | None
    media_id: str
    filename: str
    mime_type: str | None


def parse_webhook_payload(payload: dict) -> list[IncomingWhatsAppMessage]:
    messages: list[IncomingWhatsAppMessage] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for raw_message in value.get("messages", []):
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
