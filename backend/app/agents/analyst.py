"""The Analyst agent — sandboxed compute (v0.2). See SPEC.md §6.3."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.base import Agent
from app.models.schemas import Artifact


class AnalystInput(BaseModel):
    task_description: str
    dataset_refs: list[str]
    feedback: str | None = None


class AnalystOutput(BaseModel):
    code: Artifact
    figures: list[Artifact]
    log: Artifact


class Analyst(Agent[AnalystInput, AnalystOutput]):
    name = "analyst"

    async def run(self, payload: AnalystInput) -> AnalystOutput:
        # TODO (v0.2): generate Python code, execute in sandbox, capture artifacts.
        _ = payload
        raise NotImplementedError("Analyst is scheduled for v0.2")
