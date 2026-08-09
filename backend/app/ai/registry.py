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

    if name == "nvidia":
        # A provider-specific key wins over the generic one, so several
        # providers can be configured side by side.
        api_key = settings.nvidia_api_key or settings.ai_api_key
        if not api_key:
            logger.warning(
                "AI_PROVIDER=nvidia but neither NVIDIA_API_KEY nor AI_API_KEY is set -- "
                "falling back to NullProvider (deterministic behaviour unchanged)."
            )
            return NullProvider()
        try:
            # Imported lazily so this module never enters the import graph
            # unless the provider is actually selected.
            from backend.app.ai.providers.nvidia import NvidiaDocumentProvider

            return NvidiaDocumentProvider(
                api_key,
                model=settings.ai_model,
                base_url=settings.nvidia_base_url,
                timeout_seconds=settings.ai_timeout_seconds,
                max_tokens=settings.nvidia_max_tokens,
            )
        except Exception:  # noqa: BLE001 -- provider construction must never break boot
            logger.exception(
                "Could not construct the NVIDIA provider -- falling back to NullProvider."
            )
            return NullProvider()

    logger.warning(
        "AI_PROVIDER=%s is not implemented -- using NullProvider; "
        "deterministic behaviour unchanged.",
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


def shadow_mode_enabled() -> bool:
    """Observation-only analysis of documents the deterministic parser failed
    on. Independent of `document_fallback_enabled()` on purpose: shadow mode
    may run while real LLM imports stay off, and it can never write anything
    (see `backend.app.ai.shadow`)."""
    return bool(settings.ai_shadow_mode) and get_provider().name != "null"
