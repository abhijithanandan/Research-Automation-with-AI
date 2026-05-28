"""Tests for the token/cost cap (PR #5 Issue 3, BRD NFR-5).

Three layers:
  1. ``estimate_cost_usd`` — per-model pricing produces a real, non-None cost
     from token counts (the bug: cost_usd was hardcoded None so the rollup
     always summed to 0.0 and the cap could never fire).
  2. Real token counts — the gateway uses the provider's reported token
     counts (Gemini ``usage_metadata`` / Anthropic ``usage``) and only falls
     back to the word-count heuristic when the API returns no usage metadata
     (the bug: tokens were always ``len(text.split())``).
  3. ``_enforce_cost_cap`` — rolls up audit_log.cost_usd for a project and
     emits cost.cap_warn / cost.cap_exceeded against projects.token_cap_usd.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import AuditLogRow, Base, ProjectRow, UserRow, WorkflowRunRow
from app.services.llm import GeminiProvider, estimate_cost_usd

CAP_USER = UUID("7b1c2e7d-1234-4abc-8def-1234567890a1")
CAP_PROJECT = UUID("7b1c2e7d-1234-4abc-8def-1234567890a2")
CAP_RUN = UUID("7b1c2e7d-1234-4abc-8def-1234567890a3")


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


def test_estimate_cost_is_never_none_and_nonnegative() -> None:
    cost = estimate_cost_usd("gemini-3.5-flash", 1000, 1000)
    assert isinstance(cost, float)
    assert cost > 0.0


def test_estimate_cost_zero_tokens_is_zero() -> None:
    assert estimate_cost_usd("gemini-3.5-flash", 0, 0) == 0.0


def test_estimate_cost_uses_family_prefix_for_dated_variant() -> None:
    """A dated model id (gemini-2.0-flash-001) resolves to the family rate."""
    exact = estimate_cost_usd("gemini-2.0-flash", 1_000_000, 1_000_000)
    dated = estimate_cost_usd("gemini-2.0-flash-001", 1_000_000, 1_000_000)
    assert exact == dated


def test_estimate_cost_unknown_model_uses_conservative_default() -> None:
    """An un-tabulated model is priced at the high default, not zero, so it
    can't silently run a project over its cap."""
    unknown = estimate_cost_usd("some-future-model-x", 1_000_000, 0)
    # Default input rate is 5.00 / Mtok → 1M input tokens = $5.00.
    assert unknown == pytest.approx(5.00, rel=1e-6)


def test_estimate_cost_output_more_expensive_than_input() -> None:
    """Output tokens cost more than input for the same count (sanity check on
    the rate orientation)."""
    in_only = estimate_cost_usd("gemini-3.5-flash", 1_000_000, 0)
    out_only = estimate_cost_usd("gemini-3.5-flash", 0, 1_000_000)
    assert out_only > in_only


# ---------------------------------------------------------------------------
# Real Gemini token counts (PR #5 Issue 3 — was len(text.split()))
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_provider_captures_real_token_counts() -> None:
    """GeminiProvider.complete must stash the response's usage_metadata token
    counts on last_usage so the gateway uses real counts, not word counts."""
    provider = GeminiProvider(api_key="x", model="gemini-3.5-flash")

    fake_response = MagicMock()
    fake_response.text = "a short answer"
    fake_response.usage_metadata = MagicMock(
        prompt_token_count=1234,
        candidates_token_count=567,
    )

    with patch.object(
        type(provider), "client", new_callable=lambda: property(lambda self: MagicMock())
    ):
        with patch(
            "app.services.llm.asyncio.to_thread", new_callable=AsyncMock, return_value=fake_response
        ):
            text = await provider.complete("a much longer prompt than the answer")

    assert text == "a short answer"
    assert provider.last_usage == (1234, 567)


@pytest.mark.asyncio
async def test_gemini_provider_falls_back_when_no_usage_metadata() -> None:
    """When the SDK returns no usage_metadata, last_usage is None and the
    gateway is free to use the word-count fallback."""
    provider = GeminiProvider(api_key="x", model="gemini-3.5-flash")

    fake_response = MagicMock()
    fake_response.text = "answer"
    fake_response.usage_metadata = None

    with patch.object(
        type(provider), "client", new_callable=lambda: property(lambda self: MagicMock())
    ):
        with patch(
            "app.services.llm.asyncio.to_thread", new_callable=AsyncMock, return_value=fake_response
        ):
            await provider.complete("prompt")

    assert provider.last_usage is None


# ---------------------------------------------------------------------------
# _enforce_cost_cap
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(tz=UTC)
    async with factory() as setup:
        setup.add(UserRow(id=CAP_USER, firebase_uid="cap", email="cap@x.com", created_at=now))
        setup.add(
            ProjectRow(
                id=CAP_PROJECT,
                owner_id=CAP_USER,
                title="Cap Project",
                seed_query="cost cap",
                output_format="markdown",
                token_cap_usd=1.00,  # $1 budget
                status="active",
                current_phase="synthesis",
                created_at=now,
                updated_at=now,
            )
        )
        setup.add(
            WorkflowRunRow(
                id=CAP_RUN,
                project_id=CAP_PROJECT,
                phase="synthesis",
                state="awaiting_approval",
                checkpoint_id=str(CAP_RUN),
                started_at=now,
                awaiting_since=now,
                last_event_at=now,
            )
        )
        await setup.commit()

    async with factory() as s:
        yield s

    await engine.dispose()


async def _add_spend(session: AsyncSession, cost: float) -> None:
    session.add(
        AuditLogRow(
            id=uuid4(),
            project_id=CAP_PROJECT,
            workflow_run_id=CAP_RUN,
            actor="critic",
            action="agent.invoke",
            payload={"agent": "critic"},
            model="gemini-3.5-flash",
            tokens_in=100,
            tokens_out=100,
            cost_usd=cost,
            created_at=datetime.now(tz=UTC),
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_enforce_cost_cap_under_budget_no_event(session: AsyncSession) -> None:
    """Well under the cap → no warn, no exceed, returns False."""
    from app.services.workflow import _enforce_cost_cap

    await _add_spend(session, 0.10)  # $0.10 of $1.00 cap (10%)

    with patch("app.services.workflow._emit", new_callable=AsyncMock) as mock_emit:
        capped = await _enforce_cost_cap(session, CAP_PROJECT, CAP_RUN)

    assert capped is False
    mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_enforce_cost_cap_warn_threshold_emits_warn(session: AsyncSession) -> None:
    """At/above warn_pct (default 0.8) but under cap → cost.cap_warn, False."""
    from app.services.workflow import _enforce_cost_cap

    await _add_spend(session, 0.85)  # 85% of $1.00 cap

    with patch("app.services.workflow._emit", new_callable=AsyncMock) as mock_emit:
        capped = await _enforce_cost_cap(session, CAP_PROJECT, CAP_RUN)

    assert capped is False
    mock_emit.assert_called_once()
    event = mock_emit.call_args.args[1]
    assert event["type"] == "cost.cap_warn"
    assert event["spend_usd"] == pytest.approx(0.85)
    assert event["cap_usd"] == pytest.approx(1.00)


@pytest.mark.asyncio
async def test_enforce_cost_cap_over_budget_emits_exceeded(session: AsyncSession) -> None:
    """At/above the cap → cost.cap_exceeded, audit row written, returns True."""
    from sqlalchemy import select

    from app.services.workflow import _enforce_cost_cap

    await _add_spend(session, 1.20)  # over the $1.00 cap

    with patch("app.services.workflow._emit", new_callable=AsyncMock) as mock_emit:
        capped = await _enforce_cost_cap(session, CAP_PROJECT, CAP_RUN)
    await session.flush()

    assert capped is True
    event = mock_emit.call_args.args[1]
    assert event["type"] == "cost.cap_exceeded"
    assert event["spend_usd"] == pytest.approx(1.20)

    # A cost.cap_exceeded audit row must exist for the trail.
    rows = (
        (
            await session.execute(
                select(AuditLogRow).where(
                    AuditLogRow.project_id == CAP_PROJECT,
                    AuditLogRow.action == "cost.cap_exceeded",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_enforce_cost_cap_zero_cap_means_uncapped(session: AsyncSession) -> None:
    """A non-positive cap is treated as 'uncapped' — never enforce, never warn."""
    from sqlalchemy import update

    from app.services.workflow import _enforce_cost_cap

    await session.execute(
        update(ProjectRow).where(ProjectRow.id == CAP_PROJECT).values(token_cap_usd=0.0)
    )
    await _add_spend(session, 999.0)  # would blow any positive cap

    with patch("app.services.workflow._emit", new_callable=AsyncMock) as mock_emit:
        capped = await _enforce_cost_cap(session, CAP_PROJECT, CAP_RUN)

    assert capped is False
    mock_emit.assert_not_called()
