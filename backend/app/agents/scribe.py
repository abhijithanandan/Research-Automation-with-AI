"""The Scribe agent — section drafting. See SPEC.md §6.4 and docs/agents/scribe.md."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app.agents.base import Agent
from app.models.schemas import Artifact, Paper


SectionName = Literal[
    "abstract",
    "introduction",
    "related_work",
    "methodology",
    "results",
    "discussion",
    "conclusion",
]


class ScribeInput(BaseModel):
    section: SectionName
    approved_pool: list[Paper]
    prior_sections: list[Artifact]
    output_format: Literal["markdown", "latex"] = "markdown"
    feedback: str | None = None


class ScribeOutput(BaseModel):
    section: Artifact
    cited_keys: list[str]


class Scribe(Agent[ScribeInput, ScribeOutput]):
    name = "scribe"

    async def run(self, payload: ScribeInput) -> ScribeOutput:
        # TODO: RAG over approved_pool; constrain prompt to cite only approved keys.
        # TODO: validate cited_keys ⊆ {p.citation_key for p in approved_pool}.
        now = datetime.now(tz=UTC)
        project_id = payload.approved_pool[0].project_id if payload.approved_pool else uuid4()
        artifact = Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="section",
            label=payload.section,
            content=f"## {payload.section.title()}\n\nTBD",
            mime_type="text/markdown" if payload.output_format == "markdown" else "text/x-latex",
            produced_by="scribe",
            created_at=now,
        )
        return ScribeOutput(section=artifact, cited_keys=[])

    @staticmethod
    def validate_citations(cited: list[str], approved_pool: list[Paper]) -> set[str]:
        """Return the set of cited keys that are NOT in the approved pool."""
        approved = {p.citation_key for p in approved_pool}
        return set(cited) - approved
