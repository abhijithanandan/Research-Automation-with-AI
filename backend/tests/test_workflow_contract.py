"""Workflow-contract lock tests (external-review P1).

The graph now has many gates/branches (pool → synthesis → per-section drafting
→ assemble). That is correct but fragile: one transition changing silently
could produce a wrong gate, a stale phase, or a state-shape drift the UI keys
off. These tests pin the contract so any such drift turns the suite red.

Three suites:
  1. Node/gate invariants — after each resume, assert the (phase, state-ish
     fields, expected-next-node) triple. Locks the routing table.
  2. GraphState snapshot — assert the *set of keys* the state carries at each
     gate, so the large TypedDict contract can't grow/shrink unnoticed.
  3. Unknown-resume negatives — feed a garbage resume value to each gate and
     assert it defaults to *reject* (the defensive behavior the review praised).

All run on MemorySaver (no Postgres); the agents are mocked so no LLM/network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.librarian import LibrarianOutput
from app.graph.state import GraphState
from app.graph.workflow import (
    NODE_ASSEMBLE,
    NODE_AWAIT_POOL,
    NODE_AWAIT_SECTION,
    NODE_AWAIT_SYNTHESIS,
    NODE_DISCOVER,
    NODE_DRAFT,
    NODE_SYNTHESIZE,
    build_graph,
)
from app.models.schemas import Artifact, Paper, Phase

# ---------------------------------------------------------------------------
# Shared fixtures / mocks
# ---------------------------------------------------------------------------

CANONICAL_SECTIONS = [
    "abstract",
    "introduction",
    "related_work",
    "methodology",
    "results",
    "discussion",
    "conclusion",
]


def _paper(citation_key: str = "author2024") -> Paper:
    return Paper(
        id=uuid4(),
        project_id=uuid4(),
        source="arxiv",  # type: ignore[arg-type]
        external_id=f"arxiv:{citation_key}",
        title=f"Mock Paper {citation_key}",
        authors=["Author, A"],
        year=2024,
        abstract="An abstract.",
        citation_key=citation_key,
        approved=False,
        added_at=datetime.now(tz=UTC),
    )


def _critic_output(project_id):
    from app.agents.critic import CriticOutput

    now = datetime.now(tz=UTC)
    return CriticOutput(
        matrix=Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="matrix",
            label="literature-matrix",
            content='{"rows": []}',
            mime_type="application/json",
            produced_by="critic",
            created_at=now,
        ),
        summary=Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="summary",
            label="literature-summary",
            content="## Synthesis\n\nNarrative.",
            mime_type="text/markdown",
            produced_by="critic",
            created_at=now,
        ),
    )


def _scribe_output(project_id, section: str):
    from app.agents.scribe import ScribeOutput

    return ScribeOutput(
        section=Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="section",
            label=section,
            content=f"## {section}\n\nbody",
            mime_type="text/markdown",
            produced_by="scribe",
            created_at=datetime.now(tz=UTC),
        ),
        cited_keys=[],
    )


def _initial_state(project_id, *, sections_remaining=None) -> GraphState:
    return {
        "project_id": project_id,
        "workflow_run_id": uuid4(),
        "seed_query": "test query",
        "phase": Phase.DISCOVERY,
        "candidates": [],
        "approved_pool": [_paper("author2024").model_dump(mode="json")],
        "awaiting_approval": False,
        "last_feedback": None,
        "last_override": None,
        "expanded_queries": [],
        "sections_done": [],
        "sections_remaining": sections_remaining
        if sections_remaining is not None
        else ["abstract"],
        "drafts": [],
        "matrix": None,
        "summary": None,
        "synthesis_approval": None,
        "section_approval": None,
        "current_section": None,
        "manuscript": None,
        "workflow_telemetry": {},
    }


def _patched_agents():
    """Context managers patching Librarian/Critic/Scribe with cheap fakes."""
    lib = patch("app.graph.workflow.Librarian")
    crit = patch("app.graph.workflow.Critic")
    scribe = patch("app.graph.workflow.Scribe")
    return lib, crit, scribe


# ---------------------------------------------------------------------------
# Suite 1 — node/gate invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_gate_approve_routes_to_synthesize() -> None:
    """pool gate + approve → next node is synthesize, phase becomes synthesis."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    lib, crit, scribe = _patched_agents()

    with lib as ml, crit as mc, scribe as ms:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        mc.return_value.run = AsyncMock(return_value=_critic_output(project_id))
        ms.return_value.run = AsyncMock(return_value=_scribe_output(project_id, "abstract"))

        await graph.ainvoke(_initial_state(project_id), config)
        snap = await graph.aget_state(config)
        assert snap.next == (NODE_AWAIT_POOL,), "must pause at the pool gate first"

        await graph.ainvoke(Command(resume="approve"), config)
        snap = await graph.aget_state(config)

    # After approving the pool the graph runs synthesize and parks at the
    # synthesis gate; phase has advanced to SYNTHESIS.
    assert snap.next == (NODE_AWAIT_SYNTHESIS,)
    assert snap.values.get("phase") == Phase.SYNTHESIS
    assert snap.values.get("pool_approval") == "approve"


@pytest.mark.asyncio
async def test_pool_gate_reject_routes_back_to_discover() -> None:
    """pool gate + reject → re-runs discover, parks at the pool gate again."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    lib, crit, scribe = _patched_agents()

    with lib as ml, crit as mc, scribe as ms:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        mc.return_value.run = AsyncMock(return_value=_critic_output(project_id))
        ms.return_value.run = AsyncMock(return_value=_scribe_output(project_id, "abstract"))

        await graph.ainvoke(_initial_state(project_id), config)
        await graph.ainvoke(Command(resume="reject"), config)
        snap = await graph.aget_state(config)

    assert snap.next == (NODE_AWAIT_POOL,), "reject must loop back through discover to the gate"
    assert snap.values.get("pool_approval") == "reject"


@pytest.mark.asyncio
async def test_synthesis_gate_approve_routes_to_draft() -> None:
    """synthesis gate + approve → draft_section, phase DRAFTING, parks at section gate."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    lib, crit, scribe = _patched_agents()

    with lib as ml, crit as mc, scribe as ms:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        mc.return_value.run = AsyncMock(return_value=_critic_output(project_id))
        ms.return_value.run = AsyncMock(return_value=_scribe_output(project_id, "abstract"))

        await graph.ainvoke(_initial_state(project_id), config)
        await graph.ainvoke(Command(resume="approve"), config)  # pool → synthesis gate
        await graph.ainvoke(Command(resume="approve"), config)  # synthesis → section gate
        snap = await graph.aget_state(config)

    assert snap.next == (NODE_AWAIT_SECTION,)
    assert snap.values.get("phase") == Phase.DRAFTING
    assert snap.values.get("synthesis_approval") == "approve"


@pytest.mark.asyncio
async def test_section_gate_last_section_routes_to_assemble() -> None:
    """section gate + approve on the *last* remaining section → assemble → DONE."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    lib, crit, scribe = _patched_agents()

    with lib as ml, crit as mc, scribe as ms:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        mc.return_value.run = AsyncMock(return_value=_critic_output(project_id))
        ms.return_value.run = AsyncMock(
            side_effect=lambda inp: _scribe_output(project_id, inp.section)
        )

        # Single-section run so one approve at the section gate empties
        # sections_remaining and routes to assemble.
        await graph.ainvoke(_initial_state(project_id, sections_remaining=["abstract"]), config)
        await graph.ainvoke(Command(resume="approve"), config)  # pool
        await graph.ainvoke(Command(resume="approve"), config)  # synthesis
        await graph.ainvoke(Command(resume="approve"), config)  # section abstract → assemble
        snap = await graph.aget_state(config)

    # Graph ran assemble and reached the terminal node.
    assert snap.next == ()
    assert snap.values.get("phase") == Phase.DONE
    assert snap.values.get("manuscript") is not None


@pytest.mark.asyncio
async def test_section_gate_more_sections_routes_to_next_draft() -> None:
    """section gate + approve with sections remaining → draft the next section."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    lib, crit, scribe = _patched_agents()

    with lib as ml, crit as mc, scribe as ms:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        mc.return_value.run = AsyncMock(return_value=_critic_output(project_id))
        ms.return_value.run = AsyncMock(
            side_effect=lambda inp: _scribe_output(project_id, inp.section)
        )

        await graph.ainvoke(
            _initial_state(project_id, sections_remaining=["abstract", "introduction"]), config
        )
        await graph.ainvoke(Command(resume="approve"), config)  # pool
        await graph.ainvoke(Command(resume="approve"), config)  # synthesis
        await graph.ainvoke(Command(resume="approve"), config)  # abstract approved → next draft
        snap = await graph.aget_state(config)

    # Still drafting — parked at the section gate for the second section.
    assert snap.next == (NODE_AWAIT_SECTION,)
    assert snap.values.get("phase") == Phase.DRAFTING
    assert "abstract" in snap.values.get("sections_done", [])


# ---------------------------------------------------------------------------
# Suite 2 — GraphState snapshot (key-set contract)
# ---------------------------------------------------------------------------

# The full set of keys GraphState may carry. GraphState is `total=False`, so a
# key only appears once a node has written it — the set present at a gate is a
# *subset* of this. The contract this locks is the OUTER bound: no node may
# write a key outside this set (catches accidental typo-keys / silent growth),
# and the core always-present keys must be there. New intentional keys must be
# added here on purpose, which is the deliberate-review checkpoint we want.
_KNOWN_STATE_KEYS: frozenset[str] = frozenset(
    {
        "project_id",
        "workflow_run_id",
        "phase",
        "seed_query",
        "expanded_queries",
        "candidates",
        "approved_pool",
        "matrix",
        "summary",
        "synthesis_approval",
        "discovery_usage",
        "synthesis_usage",
        "sections_done",
        "sections_remaining",
        "drafts",
        "current_section",
        "section_approval",
        "manuscript",
        "drafting_usage",
        "workflow_telemetry",
        "awaiting_approval",
        "last_feedback",
        "last_override",
        "pool_approval",
    }
)

# Keys every gate snapshot must carry (set by the initial state + discover).
_CORE_STATE_KEYS: frozenset[str] = frozenset(
    {
        "project_id",
        "workflow_run_id",
        "phase",
        "seed_query",
        "candidates",
        "approved_pool",
        "awaiting_approval",
    }
)


@pytest.mark.asyncio
async def test_graphstate_shape_at_pool_gate() -> None:
    """At the pool gate, the state's key set must stay within the known
    contract (no silent growth) and carry all core keys. GraphState is
    total=False so later-phase keys (drafting_usage, etc.) are legitimately
    absent here — we assert the OUTER bound, not exact equality."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    from app.agents.librarian import LibrarianOutput

    with patch("app.graph.workflow.Librarian") as ml:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(
                candidates=[_paper().model_dump(mode="json")],
                expanded_queries=["q"],
                arxiv_categories=[],
            )
        )
        await graph.ainvoke(_initial_state(project_id), config)
        snap = await graph.aget_state(config)

    keys = set(snap.values.keys())
    unexpected = keys - _KNOWN_STATE_KEYS
    assert not unexpected, f"GraphState grew keys outside the contract: {sorted(unexpected)}"
    missing_core = _CORE_STATE_KEYS - keys
    assert not missing_core, f"GraphState missing core keys at pool gate: {sorted(missing_core)}"


@pytest.mark.asyncio
async def test_graphstate_phase_field_is_phase_enum() -> None:
    """`phase` must always be a Phase enum value, never a bare string — the
    routing + UI both depend on the enum type."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    from app.agents.librarian import LibrarianOutput

    with patch("app.graph.workflow.Librarian") as ml:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        await graph.ainvoke(_initial_state(project_id), config)
        snap = await graph.aget_state(config)

    assert isinstance(snap.values.get("phase"), Phase)


# ---------------------------------------------------------------------------
# Suite 3 — unknown-resume negatives (defensive default → reject)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", ["yes", "APPROVE", "", "true", "1", "approved", "ok"])
async def test_pool_gate_unknown_resume_defaults_to_reject(garbage: str) -> None:
    """Only the literal 'approve' advances. Any other resume value (typos,
    case variants, truthy strings, empty) must be treated as reject — the
    graph loops back through discover, NOT forward to synthesize."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    from app.agents.librarian import LibrarianOutput

    with patch("app.graph.workflow.Librarian") as ml:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        await graph.ainvoke(_initial_state(project_id), config)
        await graph.ainvoke(Command(resume=garbage), config)
        snap = await graph.aget_state(config)

    # Must NOT have advanced to synthesis; back at the pool gate, marked reject.
    assert snap.next == (NODE_AWAIT_POOL,)
    assert snap.values.get("pool_approval") == "reject"
    assert snap.values.get("phase") == Phase.DISCOVERY


@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", ["yes", "APPROVE", "", "approved"])
async def test_synthesis_gate_unknown_resume_defaults_to_reject(garbage: str) -> None:
    """Synthesis gate: unknown resume → reject (re-synthesize), never forward."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    lib, crit, scribe = _patched_agents()

    with lib as ml, crit as mc, scribe as ms:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        mc.return_value.run = AsyncMock(return_value=_critic_output(project_id))
        ms.return_value.run = AsyncMock(return_value=_scribe_output(project_id, "abstract"))

        await graph.ainvoke(_initial_state(project_id), config)
        await graph.ainvoke(Command(resume="approve"), config)  # → synthesis gate
        await graph.ainvoke(Command(resume=garbage), config)  # garbage at synthesis gate
        snap = await graph.aget_state(config)

    assert snap.next == (NODE_AWAIT_SYNTHESIS,), "must re-synthesize and re-park, not draft"
    assert snap.values.get("synthesis_approval") == "reject"


@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", ["yes", "APPROVE", "", "approved"])
async def test_section_gate_unknown_resume_defaults_to_reject(garbage: str) -> None:
    """Section gate: unknown resume → reject (re-draft same section), never advance."""
    project_id = uuid4()
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    lib, crit, scribe = _patched_agents()

    with lib as ml, crit as mc, scribe as ms:
        ml.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        mc.return_value.run = AsyncMock(return_value=_critic_output(project_id))
        ms.return_value.run = AsyncMock(
            side_effect=lambda inp: _scribe_output(project_id, inp.section)
        )

        await graph.ainvoke(
            _initial_state(project_id, sections_remaining=["abstract", "introduction"]), config
        )
        await graph.ainvoke(Command(resume="approve"), config)  # pool
        await graph.ainvoke(Command(resume="approve"), config)  # synthesis → section gate
        await graph.ainvoke(Command(resume=garbage), config)  # garbage at section gate
        snap = await graph.aget_state(config)

    # The reject re-runs node_draft_section, which resets section_approval to
    # None for a fresh decision — so the *observable* invariant is: we are
    # back at the section gate, on the SAME section, and nothing was marked
    # done (the rejected section stays pending). That's the safety property.
    assert snap.next == (NODE_AWAIT_SECTION,), "must re-draft the same section, not advance"
    assert "abstract" not in snap.values.get("sections_done", [])
    assert snap.values.get("current_section") == "abstract"


# ---------------------------------------------------------------------------
# Graph wiring sanity — the node/edge table itself
# ---------------------------------------------------------------------------


def test_graph_registers_exactly_the_expected_nodes() -> None:
    """Lock the node set so a node can't be added/removed without updating
    this contract test."""
    graph = build_graph(MemorySaver())
    expected = {
        NODE_DISCOVER,
        NODE_AWAIT_POOL,
        NODE_SYNTHESIZE,
        NODE_AWAIT_SYNTHESIS,
        NODE_DRAFT,
        NODE_AWAIT_SECTION,
        NODE_ASSEMBLE,
    }
    actual = set(graph.nodes) - {"__start__", "__end__"}
    assert actual == expected, f"graph node set drifted: {actual ^ expected}"
