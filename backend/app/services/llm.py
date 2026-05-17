"""LLM provider gateway. All inference flows through this module.

Only the Gemini provider is wired in Phase 1 (v0.1). The `LLMProvider`
Protocol allows v0.2 to plug in OpenAI / Anthropic / DeepSeek without
changing any caller code. Every call is counted for tokens + cost and
writes to `audit_log` (see AuditLogEntry in schemas.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from google import genai

from app.config import get_settings
from app.utils.logging import get_logger

_log = get_logger(__name__)


class LLMProvider(Protocol):
    async def complete(self, prompt: str, **kwargs: object) -> str: ...
    async def stream(self, prompt: str, **kwargs: object) -> AsyncIterator[str]: ...


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------


class GeminiProvider:
    """Wraps google-genai for use as an LLMProvider."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model_name = model

    async def complete(self, prompt: str, **kwargs: object) -> str:
        # Offload blocking SDK call to a thread so we don't block the event loop.
        from google.genai.types import GenerateContentConfig

        config = kwargs.get("config")
        if (
            config is not None
            and not isinstance(config, dict)
            and not isinstance(config, GenerateContentConfig)
        ):
            config = None

        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model_name,
            contents=prompt,
            config=config,  # type: ignore[arg-type]
        )
        return response.text or ""

    async def stream(self, prompt: str, **kwargs: object) -> AsyncIterator[str]:
        # google-genai streaming via generate_content_stream.
        response_stream = await asyncio.to_thread(
            self._client.models.generate_content_stream,
            model=self._model_name,
            contents=prompt,
        )

        async def _gen() -> AsyncIterator[str]:
            for chunk in response_stream:
                if chunk.text:
                    yield chunk.text

        return _gen()


# ---------------------------------------------------------------------------
# Gateway — single entry point for all LLM calls
# ---------------------------------------------------------------------------


class LLMGateway:
    """Route all LLM calls through a single provider, tracking tokens + cost.

    Token / cost telemetry is accumulated per call and returned so that callers
    can write `AuditLogEntry` rows. The gateway itself doesn't write to the DB
    because it doesn't have access to the DB session; that responsibility lies
    with the agent that calls it.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if settings.llm_provider == "gemini":
            self._provider: LLMProvider = GeminiProvider(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
            )
        else:
            # v0.2: wire OpenAI / Anthropic / DeepSeek here.
            raise NotImplementedError(
                f"LLM provider '{settings.llm_provider}' not yet supported. "
                "Set LLM_PROVIDER=gemini for Phase 1."
            )

    async def complete(self, prompt: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        """Complete a prompt. Returns (text, telemetry_dict).

        Callers must write the telemetry to audit_log — see agents/*.py.
        """
        _log.debug("llm_complete_start", prompt_len=len(prompt))
        text = await self._provider.complete(prompt, **kwargs)
        telemetry: dict[str, object] = {
            "tokens_in": len(prompt.split()),  # rough estimate until SDK exposes counts
            "tokens_out": len(text.split()),
            "cost_usd": None,  # TODO: wire real cost once Gemini SDK exposes it
        }
        _log.debug("llm_complete_done", tokens_out=telemetry["tokens_out"])
        return text, telemetry

    async def stream(self, prompt: str, **kwargs: object) -> AsyncIterator[str]:
        """Stream tokens from the provider. Callers forward deltas over WS."""
        _log.debug("llm_stream_start", prompt_len=len(prompt))
        return await self._provider.stream(prompt, **kwargs)


# Module-level singleton — imported by agents.
_gateway: LLMGateway | None = None


def get_llm_gateway() -> LLMGateway:
    """Return the module-level gateway, creating it on first call."""
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway
