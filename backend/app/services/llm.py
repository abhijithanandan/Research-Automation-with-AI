"""LLM provider gateway. All inference flows through this module."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.config import get_settings


class LLMProvider(Protocol):
    async def complete(self, prompt: str, **kwargs: object) -> str: ...
    async def stream(self, prompt: str, **kwargs: object) -> AsyncIterator[str]: ...


class LLMGateway:
    """Single entry point for LLM calls. Tracks tokens + cost and emits audit events."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def complete(self, prompt: str, **kwargs: object) -> str:
        # TODO: dispatch to provider, count tokens, write audit_log.
        _ = prompt, kwargs
        raise NotImplementedError("LLMGateway.complete: wire up a provider")

    async def stream(self, prompt: str, **kwargs: object) -> AsyncIterator[str]:
        # TODO: dispatch streaming; forward deltas; aggregate at end for audit_log.
        _ = prompt, kwargs
        raise NotImplementedError("LLMGateway.stream: wire up a provider")
        yield ""  # pragma: no cover  (typing satisfier)
