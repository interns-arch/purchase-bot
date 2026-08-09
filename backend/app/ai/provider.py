"""The provider contract. Business logic depends ONLY on this Protocol and on
`schemas.py` -- never on a vendor SDK -- so Gemini / OpenAI / a local Ollama
model / anything else can be swapped by configuration alone.

Contract rules:
- Every method returns `None` on ANY failure (not configured, timeout, bad
  JSON, quota, network). Callers treat `None` as "deterministic path stands"
  and fall back to NEEDS_REVIEW. A provider must never raise into business code.
- A provider never touches the database, never resolves identity, and never
  computes stock/allocation. It only proposes a reading of bytes/text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from backend.app.ai.schemas import Intent, NormalizedDocument


@dataclass
class UnderstandRequest:
    """What the provider is asked to interpret.

    `compact_text` is a token-bounded textual rendering of the document (see
    `compact.py`) -- never the raw file -- so cost stays predictable and no
    unnecessary data leaves the network.
    """

    document_type: str  # one of schemas.DOCUMENT_TYPES -- decided by the BACKEND
    compact_text: str
    file_name: str
    hints: dict[str, str] = field(default_factory=dict)


@dataclass
class IntentContext:
    """Non-authoritative hints for intent extraction. Codes/names here are for
    disambiguation only; the backend re-validates every entity afterwards."""

    known_customer_codes: list[str] = field(default_factory=list)
    known_vendor_codes: list[str] = field(default_factory=list)


@runtime_checkable
class DocumentUnderstandingProvider(Protocol):
    name: str

    def understand_document(self, request: UnderstandRequest) -> NormalizedDocument | None:
        """Propose a normalized reading, or None if it cannot."""
        ...

    def extract_intent(self, text: str, context: IntentContext) -> Intent | None:
        """Map a natural-language message to a closed-set Intent, or None."""
        ...


class NullProvider:
    """The default. Understands nothing, so the application behaves exactly as
    it did before this layer existed. Keeping this as the default means the
    feature is inert until someone deliberately configures a real provider."""

    name = "null"

    def understand_document(self, request: UnderstandRequest) -> NormalizedDocument | None:
        return None

    def extract_intent(self, text: str, context: IntentContext) -> Intent | None:
        return None
