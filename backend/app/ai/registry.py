"""Configuration-driven provider selection.

`get_provider()` is the ONLY place business code obtains a provider. It always
returns something usable -- `NullProvider` when AI is unset, unknown,
misconfigured, or when a real provider's SDK/credentials are unavailable -- so
no caller has to special-case "AI not configured".

Never logs or echoes the API key.
"""

from __future__ import annotations

from backend.app.ai.provider import DocumentUnderstandingProvider, NullProvider
from backend.app.core.config import settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

_provider: DocumentUnderstandingProvider | None = None


def _build_provider() -> DocumentUnderstandingProvider:
    name = (settings.ai_provider or "null").strip().lower()

    if name in ("", "null", "none", "off", "disabled"):
        return NullProvider()

    if not settings.ai_api_key and name != "ollama":
        logger.warning(
            "AI_PROVIDER=%s but AI_API_KEY is not set -- falling back to NullProvider "
            "(deterministic behaviour unchanged).",
            name,
        )
        return NullProvider()

    # Real providers are added in Phase 3+. Importing them lazily HERE keeps
    # every vendor SDK out of the import graph until it is actually selected.
    logger.warning(
        "AI_PROVIDER=%s is not implemented yet (Phase 1 is scaffolding only) -- "
        "using NullProvider; deterministic behaviour unchanged.",
        name,
    )
    return NullProvider()


def get_provider() -> DocumentUnderstandingProvider:
    """Process-wide provider (built once). Restart to pick up config changes,
    consistent with how the rest of this app reads settings."""
    global _provider
    if _provider is None:
        _provider = _build_provider()
        logger.info("Document-understanding provider: %s", _provider.name)
    return _provider


def reset_provider_cache() -> None:
    """Test hook -- forces the next `get_provider()` to rebuild from settings."""
    global _provider
    _provider = None


def document_fallback_enabled() -> bool:
    """True only when BOTH the feature flag is on AND a non-null provider is
    active. Phase 2+ import hooks must consult this before any model call."""
    return bool(settings.ai_fallback_enabled) and get_provider().name != "null"


def intent_enabled() -> bool:
    return bool(settings.ai_intent_enabled) and get_provider().name != "null"
