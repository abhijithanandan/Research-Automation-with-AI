"""LLM gateway retry behaviour.

Regression for the observed failure where a single transient
``httpx.ConnectError`` ("[Errno 111] Connection refused") from the Gemini
provider mid-Scribe killed an entire multi-phase workflow run at Phase 4,
discarding all the completed Phase 1-2 work. The gateway now wraps the
provider call in a bounded tenacity retry on transport-level errors only.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.llm import LLMGateway


class _FlakyProvider:
    """Fails with a transient transport error `fail_times` times, then succeeds.

    `last_usage` mirrors the real providers so the gateway's telemetry path
    works unchanged.
    """

    def __init__(self, fail_times: int, exc: Exception) -> None:
        self.calls = 0
        self._fail_times = fail_times
        self._exc = exc
        self.last_usage: tuple[int, int] | None = (10, 5)

    async def complete(self, prompt: str, **kwargs: object) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return "drafted section text"

    async def stream(self, prompt: str, **kwargs: object):  # pragma: no cover
        yield "x"


def _gateway_with(provider: _FlakyProvider) -> LLMGateway:
    """Build a gateway and swap in the fake provider (skips settings/network)."""
    gw = LLMGateway.__new__(LLMGateway)  # bypass __init__ (no real provider)
    gw._provider = provider  # type: ignore[attr-defined]
    gw._model_name = "gemini-3.5-flash"  # type: ignore[attr-defined]
    return gw


@pytest.mark.asyncio
async def test_complete_retries_then_succeeds_on_transient_connect_error() -> None:
    """Two transient ConnectErrors, then success → 3 calls, result returned."""
    provider = _FlakyProvider(fail_times=2, exc=httpx.ConnectError("Connection refused"))
    gw = _gateway_with(provider)

    text, telemetry = await gw.complete("write the abstract")

    assert text == "drafted section text"
    assert provider.calls == 3  # 2 failures + 1 success
    assert telemetry["model"] == "gemini-3.5-flash"
    assert telemetry["tokens_in"] == 10
    assert telemetry["tokens_out"] == 5


@pytest.mark.asyncio
async def test_complete_gives_up_after_three_transient_failures() -> None:
    """Persistent transport failure → reraises the ORIGINAL exception (not
    tenacity's RetryError) after exactly 3 attempts, so the graph-resume
    handler sees the real ConnectError."""
    provider = _FlakyProvider(fail_times=99, exc=httpx.ConnectError("Connection refused"))
    gw = _gateway_with(provider)

    with pytest.raises(httpx.ConnectError):
        await gw.complete("write the abstract")
    assert provider.calls == 3  # stop_after_attempt(3)


@pytest.mark.asyncio
async def test_complete_does_not_retry_non_transient_errors() -> None:
    """A non-transport error (e.g. ValueError from a bad API key / 400) must
    fail fast — retrying auth/validation errors wastes quota and latency."""
    provider = _FlakyProvider(fail_times=99, exc=ValueError("invalid api key"))
    gw = _gateway_with(provider)

    with pytest.raises(ValueError):
        await gw.complete("write the abstract")
    assert provider.calls == 1  # no retry on non-transient errors
