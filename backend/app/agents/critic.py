"""The Critic agent — synthesis. See SPEC.md §6.2 and docs/agents/critic.md."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from app.agents.base import Agent
from app.models.schemas import Artifact, Paper


class CriticInput(BaseModel):
    approved_papers: list[Paper]
    focus: str | None = None
    feedback: str | None = None


class CriticOutput(BaseModel):
    matrix: Artifact
    summary: Artifact


class Critic(Agent[CriticInput, CriticOutput]):
    name = "critic"

    async def run(self, payload: CriticInput) -> CriticOutput:
        # TODO: build comparison matrix via structured LLM call.
        # TODO: produce narrative summary via RAG over the approved pool.
        now = datetime.now(tz=UTC)
        project_id = payload.approved_papers[0].project_id if payload.approved_papers else uuid4()
        matrix = Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="matrix",
            label="literature-matrix",
            content="{}",
            mime_type="application/json",
            produced_by="critic",
            created_at=now,
        )
        summary = Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="summary",
            label="literature-summary",
            content="# Summary\n\nTBD",
            mime_type="text/markdown",
            produced_by="critic",
            created_at=now,
        )
        return CriticOutput(matrix=matrix, summary=summary)
