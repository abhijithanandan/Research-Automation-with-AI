"""LLM provider gateway. All inference flows through this module.

Two providers are wired: Gemini (default) and Anthropic Claude. The
``LLMProvider`` Protocol allows v0.2 to plug in OpenAI / DeepSeek without
changing any caller code. Every call is counted for tokens + cost and
writes to ``audit_log`` (see AuditLogEntry in schemas.py).

Telemetry note: Anthropic's API returns exact token counts in the
``usage`` field. The gateway uses those when present and falls back to the
word-count heuristic only for providers (like Gemini today) that don't
surface real counts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

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
        self._api_key = api_key
        self._model_name = model
        self._client: genai.Client | None = None
        # Stash of the most recent (prompt_tokens, candidates_tokens) from the
        # response's usage_metadata so the gateway can use real token counts
        # instead of the word-count heuristic (PR #5 finding: tokens were
        # len(text.split())). None when the SDK didn't return usage metadata.
        self.last_usage: tuple[int, int] | None = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

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
            self.client.models.generate_content,
            model=self._model_name,
            contents=prompt,
            config=config,  # type: ignore[arg-type]
        )

        # Capture exact token counts from the response usage metadata. The
        # google-genai SDK exposes prompt_token_count / candidates_token_count
        # on response.usage_metadata; both can be None on some responses, so
        # we coerce defensively and only stash when at least one is present.
        self.last_usage = None
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            prompt_tokens = getattr(usage, "prompt_token_count", None)
            out_tokens = getattr(usage, "candidates_token_count", None)
            if prompt_tokens is not None or out_tokens is not None:
                self.last_usage = (int(prompt_tokens or 0), int(out_tokens or 0))

        return response.text or ""

    async def stream(self, prompt: str, **kwargs: object) -> AsyncIterator[str]:
        # google-genai streaming via generate_content_stream. The returned
        # iterator is *synchronous* and each next() makes a blocking network
        # read — iterating it from the event loop would freeze the entire
        # async runtime for the duration of the stream (coderabbit PR #5
        # finding). Pump it through asyncio.to_thread one chunk at a time.
        response_stream = await asyncio.to_thread(
            self.client.models.generate_content_stream,
            model=self._model_name,
            contents=prompt,
        )

        _sentinel = object()

        def _next_chunk() -> object:
            try:
                return next(response_stream)
            except StopIteration:
                return _sentinel

        async def _gen() -> AsyncIterator[str]:
            while True:
                chunk = await asyncio.to_thread(_next_chunk)
                if chunk is _sentinel:
                    break
                text = getattr(chunk, "text", None)
                if text:
                    yield text

        return _gen()


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------


# Conservative max_tokens for Claude. Anthropic's Messages API requires
# max_tokens; we set a generous default that covers the longest sections we
# produce (the Scribe's related_work / discussion sections are the longest
# single calls in the system, typically <4k output tokens).
_ANTHROPIC_DEFAULT_MAX_TOKENS = 8192


class AnthropicProvider:
    """Wraps anthropic's AsyncAnthropic for use as an LLMProvider.

    Notes on token counting (BRD FR-3.3 / NFR-5):
      - The Messages API returns ``response.usage.input_tokens`` and
        ``output_tokens`` directly — we surface them via the
        ``last_usage`` attribute so the gateway can use them in telemetry
        instead of the word-count heuristic.
      - Cost estimation is left to the gateway because it depends on the
        model tier (Opus vs Sonnet vs Haiku) and Anthropic's pricing
        table, which we don't bundle here.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model_name = model
        self._client: Any = None
        # Stash of the most recent (input_tokens, output_tokens) so the
        # gateway can read it without our complete() needing to change
        # the LLMProvider return type (still str, like Gemini).
        self.last_usage: tuple[int, int] | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def complete(self, prompt: str, **kwargs: object) -> str:
        max_tokens_arg = kwargs.get("max_tokens")
        max_tokens = (
            int(max_tokens_arg)
            if isinstance(max_tokens_arg, int)
            else _ANTHROPIC_DEFAULT_MAX_TOKENS
        )

        response = await self.client.messages.create(
            model=self._model_name,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        # Anthropic returns a list of content blocks. For plain text prompts
        # the first block is always a TextBlock; concatenate any extras
        # defensively in case the API ever returns multi-block output.
        parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        text_out = "".join(parts)

        # Capture exact token counts for the gateway's telemetry.
        # CodeRabbit: reset BEFORE inspecting so a missing usage field can't
        # leave the previous call's counts in self.last_usage. Stale token
        # counts feed straight into the cost-cap rollup (NFR-5) and skew it.
        self.last_usage = None
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = (
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
            )
        return text_out

    async def stream(self, prompt: str, **kwargs: object) -> AsyncIterator[str]:
        max_tokens_arg = kwargs.get("max_tokens")
        max_tokens = (
            int(max_tokens_arg)
            if isinstance(max_tokens_arg, int)
            else _ANTHROPIC_DEFAULT_MAX_TOKENS
        )

        async def _gen() -> AsyncIterator[str]:
            async with self.client.messages.stream(
                model=self._model_name,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for chunk in stream.text_stream:
                    if chunk:
                        yield chunk

        return _gen()


# ---------------------------------------------------------------------------
# Pricing — per-model USD rates for the token/cost rollup (BRD NFR-5)
# ---------------------------------------------------------------------------

# Published list prices in USD per 1 *million* tokens, (input, output).
# Sources: Google AI pricing + Anthropic pricing pages (as of 2026-05).
# Keys are matched as a case-insensitive *prefix* of the model id so dated
# variants (e.g. "gemini-2.0-flash-001") resolve to the family rate. Update
# this table when a provider revises pricing or we add a model. A model that
# matches no prefix falls back to `_DEFAULT_PRICE_PER_MTOK` so cost is
# *estimated high* rather than silently dropped to zero (the bug PR #5
# flagged — cost_usd was always None so the cap could never fire).
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    # Gemini
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # Anthropic Claude
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
}

# Conservative fallback for an unknown model — priced at the high end so an
# un-tabulated model can't quietly run the project over its cap. Operators
# who see inflated cost for a new model should add it to _PRICE_PER_MTOK.
_DEFAULT_PRICE_PER_MTOK: tuple[float, float] = (5.00, 15.00)


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate the USD cost of one LLM call from token counts.

    Returns a non-negative float (never None) so the per-project rollup in
    audit_log always has a real number to sum. Resolves the model to a
    pricing-table family by case-insensitive prefix match; unknown models
    use the conservative default rate.
    """
    model_l = (model or "").lower()
    rate_in, rate_out = _DEFAULT_PRICE_PER_MTOK
    for prefix, (r_in, r_out) in _PRICE_PER_MTOK.items():
        if model_l.startswith(prefix):
            rate_in, rate_out = r_in, r_out
            break
    cost = (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out
    return max(0.0, cost)


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
            self._model_name: str = settings.llm_model
        elif settings.llm_provider == "anthropic":
            self._provider = AnthropicProvider(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
            )
            self._model_name = settings.llm_model
        else:
            # v0.2: wire OpenAI / DeepSeek here.
            raise NotImplementedError(
                f"LLM provider '{settings.llm_provider}' not yet supported. "
                "Set LLM_PROVIDER=gemini or LLM_PROVIDER=anthropic."
            )

    @property
    def model_name(self) -> str:
        """The active model identifier — written to audit_log (BRD FR-3.3)."""
        return self._model_name

    async def complete(self, prompt: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        """Complete a prompt. Returns (text, telemetry_dict).

        Callers must write the telemetry to audit_log — see agents/*.py.
        """
        _log.debug("llm_complete_start", prompt_len=len(prompt))
        text = await self._provider.complete(prompt, **kwargs)

        # Prefer exact token counts from the provider when available. Both the
        # Anthropic and Gemini providers stash real counts on `last_usage`
        # (from the API's usage / usage_metadata). The word-count heuristic is
        # only a fallback for the rare response that carries no usage metadata.
        last_usage = getattr(self._provider, "last_usage", None)
        if isinstance(last_usage, tuple) and len(last_usage) == 2:
            tokens_in, tokens_out = last_usage
        else:
            tokens_in = len(prompt.split())
            tokens_out = len(text.split())

        telemetry: dict[str, object] = {
            "model": self._model_name,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            # Real per-model cost estimate (BRD NFR-5). Previously hardcoded
            # None, which made the per-project rollup always sum to 0.0 and
            # the token cap unenforceable (PR #5 finding).
            "cost_usd": estimate_cost_usd(self._model_name, tokens_in, tokens_out),
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
