"""Tests for the Phase 4 per-section HITL gate (Phase 4 B-4 / B-5).

SPEC §5.2 / docs/workflow/state-machine.md:

After the Phase 2 synthesis is approved, the graph drafts the abstract,
pauses at `node_await_section_approval`, and emits an `approval.required`
event with phase="drafting" and the current section name. Each subsequent
approve advances to the next section; reject re-runs the current section;
override marks the section produced_by="human" and advances.

The seven canonical sections (BRD FR-2.4) are:
    abstract → introduction → related_work → methodology → results
    → discussion → conclusion

After the conclusion approves the graph runs `node_assemble`, persists the
manuscript artifact, and emits state.changed{phase:"done"}.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.critic import CriticOutput
from app.agents.librarian import LibrarianOutput
from app.agents.scribe import ScribeOutput
from app.graph.workflow import (
    NODE_ASSEMBLE,
    NODE_AWAIT_SECTION,
    NODE_DRAFT,
    build_graph,
)
from app.models.schemas import Artifact, Phase

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000040")

SECTIONS = [
    "abstract",
    "introduction",
    "related_work",
    "methodology",
    "results",
    "discussion",
    "conclusion",
]


def _critic_output() -> CriticOutput:
    now = datetime.now(tz=UTC)
    return CriticOutput(
        matrix=Artifact(
            id=uuid4(),
            project_id=TEST_PROJECT_ID,
            kind="matrix",
            label="literature-matrix",
            content='{"rows": []}',
            mime_type="application/json",
            produced_by="critic",
            created_at=now,
        ),
        summary=Artifact(
            id=uuid4(),
            project_id=TEST_PROJECT_ID,
            kind="summary",
            label="literature-summary",
            content="## Synthesis\n\nNarrative.",
            mime_type="text/markdown",
            produced_by="critic",
            created_at=now,
        ),
    )


def _scribe_output(section: str, cited: list[str] | None = None) -> ScribeOutput:
    now = datetime.now(tz=UTC)
    return ScribeOutput(
        section=Artifact(
            id=uuid4(),
            project_id=TEST_PROJECT_ID,
            kind="section",
            label=section,
            content=f"## {section.title()}\n\nDrafted prose for {section}.",
            mime_type="text/markdown",
            produced_by="scribe",
            created_at=now,
        ),
        cited_keys=cited or [],
    )


def _initial_state() -> dict[str, object]:
    return {
        "project_id": TEST_PROJECT_ID,
        "workflow_run_id": uuid4(),
        "seed_query": "test",
        "phase": Phase.DISCOVERY,
        "candidates": [],
        "approved_pool": [],
        "awaiting_approval": False,
        "last_feedback": None,
        "last_override": None,
        "expanded_queries": [],
        "sections_done": [],
        "sections_remaining": list(SECTIONS),
        "drafts": [],
        "matrix": None,
        "summary": None,
        "synthesis_approval": None,
        "current_section": None,
        "section_approval": None,
        "manuscript": None,
    }


# ---------------------------------------------------------------------------
# Graph registration
# ---------------------------------------------------------------------------


def test_graph_registers_section_gate_node() -> None:
    """The compiled graph must contain the per-section gate + assemble nodes."""
    graph = build_graph(MemorySaver())
    assert NODE_DRAFT in graph.nodes
    assert NODE_AWAIT_SECTION in graph.nodes
    assert NODE_ASSEMBLE in graph.nodes


# ---------------------------------------------------------------------------
# B-8 required gate / loop / override tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_interrupts_at_first_section_after_synthesis_approve() -> None:
    """After synthesis-approve the graph drafts the abstract and parks at the
    section gate with current_section='abstract'."""
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    with (
        patch("app.graph.workflow.Librarian") as lib,
        patch("app.graph.workflow.Critic") as crit,
        patch("app.graph.workflow.Scribe") as scr,
    ):
        lib.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        crit.return_value.run = AsyncMock(return_value=_critic_output())
        scr.return_value.run = AsyncMock(return_value=_scribe_output("abstract"))

        await graph.ainvoke(_initial_state(), config)  # → pool gate
        await graph.ainvoke(Command(resume="approve"), config)  # → synthesis gate
        await graph.ainvoke(Command(resume="approve"), config)  # → section gate (abstract)
        snapshot = await graph.aget_state(config)

    assert snapshot.next == (NODE_AWAIT_SECTION,)
    assert snapshot.values.get("phase") == Phase.DRAFTING
    assert snapshot.values.get("current_section") == "abstract"
    # drafts list now has the abstract entry.
    drafts = snapshot.values.get("drafts") or []
    assert len(drafts) == 1
    assert drafts[0]["section"] == "abstract"


@pytest.mark.asyncio
async def test_approve_section_advances_to_next() -> None:
    """Approving the abstract must produce the introduction at the next gate."""
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    with (
        patch("app.graph.workflow.Librarian") as lib,
        patch("app.graph.workflow.Critic") as crit,
        patch("app.graph.workflow.Scribe") as scr,
    ):
        lib.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        crit.return_value.run = AsyncMock(return_value=_critic_output())
        # Scribe returns whichever section was requested.
        scribe_calls: list[str] = []

        async def scribe_run(payload):  # type: ignore[no-untyped-def]
            scribe_calls.append(payload.section)
            return _scribe_output(payload.section)

        scr.return_value.run = AsyncMock(side_effect=scribe_run)

        await graph.ainvoke(_initial_state(), config)
        await graph.ainvoke(Command(resume="approve"), config)  # pool → synthesis
        await graph.ainvoke(Command(resume="approve"), config)  # synthesis → abstract gate
        # Approve abstract → graph drafts introduction, parks at section gate.
        await graph.ainvoke(Command(resume="approve"), config)
        snapshot = await graph.aget_state(config)

    assert snapshot.next == (NODE_AWAIT_SECTION,)
    assert snapshot.values.get("current_section") == "introduction"
    assert scribe_calls == ["abstract", "introduction"]


@pytest.mark.asyncio
async def test_reject_section_reruns_same_section() -> None:
    """Rejecting the abstract with feedback re-drafts the abstract (not introduction)."""
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    with (
        patch("app.graph.workflow.Librarian") as lib,
        patch("app.graph.workflow.Critic") as crit,
        patch("app.graph.workflow.Scribe") as scr,
    ):
        lib.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        crit.return_value.run = AsyncMock(return_value=_critic_output())
        seen: list[tuple[str, str | None]] = []

        async def scribe_run(payload):  # type: ignore[no-untyped-def]
            seen.append((payload.section, payload.feedback))
            return _scribe_output(payload.section)

        scr.return_value.run = AsyncMock(side_effect=scribe_run)

        await graph.ainvoke(_initial_state(), config)
        await graph.ainvoke(Command(resume="approve"), config)
        await graph.ainvoke(Command(resume="approve"), config)
        # Reject the abstract with feedback.
        await graph.ainvoke(
            Command(resume="reject", update={"last_feedback": "Make it shorter."}),
            config,
        )
        snapshot = await graph.aget_state(config)

    # The graph re-drafted the abstract; current_section is still abstract.
    assert snapshot.values.get("current_section") == "abstract"
    # Scribe called twice for abstract; second call carried the feedback.
    abstract_calls = [s for s in seen if s[0] == "abstract"]
    assert len(abstract_calls) == 2
    assert abstract_calls[1][1] == "Make it shorter."


@pytest.mark.asyncio
async def test_override_section_records_human_artifact_and_advances() -> None:
    """Override-the-section path: human-edited content replaces the Scribe draft,
    drafts[-1]['artifact']['produced_by'] == 'human', graph advances to the
    next section."""
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    human_section = {
        "id": str(uuid4()),
        "project_id": str(TEST_PROJECT_ID),
        "kind": "section",
        "label": "abstract",
        "content": "## Abstract\n\nHand-written replacement.",
        "mime_type": "text/markdown",
        "produced_by": "human",
        "parent_id": None,
        "created_at": "2026-05-26T00:00:00+00:00",
    }

    with (
        patch("app.graph.workflow.Librarian") as lib,
        patch("app.graph.workflow.Critic") as crit,
        patch("app.graph.workflow.Scribe") as scr,
    ):
        lib.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        crit.return_value.run = AsyncMock(return_value=_critic_output())

        async def scribe_run(payload):  # type: ignore[no-untyped-def]
            return _scribe_output(payload.section)

        scr.return_value.run = AsyncMock(side_effect=scribe_run)

        await graph.ainvoke(_initial_state(), config)
        await graph.ainvoke(Command(resume="approve"), config)
        await graph.ainvoke(Command(resume="approve"), config)
        # Override the abstract — approve + last_override.
        await graph.ainvoke(
            Command(resume="approve", update={"last_override": human_section}),
            config,
        )
        snapshot = await graph.aget_state(config)

    drafts = snapshot.values.get("drafts") or []
    # The abstract draft entry now reflects the human override.
    abstract_entry = next(d for d in drafts if d["section"] == "abstract")
    assert abstract_entry["artifact"]["produced_by"] == "human"
    assert "Hand-written replacement" in abstract_entry["artifact"]["content"]
    # Graph advanced — current_section is the next section (introduction).
    assert snapshot.values.get("current_section") == "introduction"
    # Override is cleared so a later gate cannot re-consume it.
    assert snapshot.values.get("last_override") is None


@pytest.mark.asyncio
async def test_all_sections_approved_routes_to_assemble_and_emits_done() -> None:
    """Approving all seven sections must trigger node_assemble; manuscript is
    populated; the graph runs to completion (snapshot.next empty)."""
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    with (
        patch("app.graph.workflow.Librarian") as lib,
        patch("app.graph.workflow.Critic") as crit,
        patch("app.graph.workflow.Scribe") as scr,
    ):
        lib.return_value.run = AsyncMock(
            return_value=LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        crit.return_value.run = AsyncMock(return_value=_critic_output())

        async def scribe_run(payload):  # type: ignore[no-untyped-def]
            return _scribe_output(payload.section)

        scr.return_value.run = AsyncMock(side_effect=scribe_run)

        await graph.ainvoke(_initial_state(), config)
        await graph.ainvoke(Command(resume="approve"), config)  # pool → synthesis
        await graph.ainvoke(Command(resume="approve"), config)  # synthesis → abstract gate
        # Approve all seven sections.
        for _ in SECTIONS:
            await graph.ainvoke(Command(resume="approve"), config)
        snapshot = await graph.aget_state(config)

    # Graph ran to completion.
    assert not snapshot.next
    # Manuscript was assembled.
    manuscript = snapshot.values.get("manuscript")
    assert manuscript is not None
    assert manuscript["kind"] == "manuscript"
    # sections_done has all seven in canonical order.
    assert snapshot.values.get("sections_done") == SECTIONS
    assert snapshot.values.get("sections_remaining") == []
