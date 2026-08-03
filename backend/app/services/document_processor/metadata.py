"""Shared context passed into `process_document`, filled in differently by
the manual-upload glue and the WhatsApp worker -- the processing engine
itself never needs to know which one is calling it."""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.documents.models import IncomingDocumentType


@dataclass
class DocumentMetadata:
    sender: str | None = None
    caption: str | None = None
    document_type_hint: IncomingDocumentType | None = None
    vendor_id_hint: int | None = None
    external_message_id: str | None = None
    original_filename: str | None = None
