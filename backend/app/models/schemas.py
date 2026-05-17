"""Pydantic wire types. These are the canonical shapes from SPEC.md §2.2."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, HttpUrl


class Phase(str, Enum):
    DISCOVERY = "discovery"
    SYNTHESIS = "synthesis"
    ANALYSIS = "analysis"
    DRAFTING = "drafting"
    DONE = "done"


class User(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str | None = None
    created_at: datetime


class Project(BaseModel):
    id: UUID
    owner_id: UUID
    title: str
    seed_query: str
    output_format: Literal["markdown", "latex"] = "markdown"
    token_cap_usd: float = 5.0
    status: Literal["draft", "active", "completed", "archived"] = "draft"
    current_phase: Phase = Phase.DISCOVERY
    created_at: datetime
    updated_at: datetime


class WorkflowRun(BaseModel):
    id: UUID
    project_id: UUID
    phase: Phase
    state: Literal["running", "awaiting_approval", "approved", "rejected", "error"]
    checkpoint_id: str
    started_at: datetime
    awaiting_since: datetime | None = None
    last_event_at: datetime


class Paper(BaseModel):
    id: UUID
    project_id: UUID
    source: Literal["semantic_scholar", "arxiv", "crossref", "upload"]
    external_id: str
    title: str
    authors: list[str]
    year: int | None = None
    abstract: str | None = None
    pdf_url: HttpUrl | None = None
    citation_key: str
    citation_count: int | None = None
    approved: bool = False
    added_at: datetime


ArtifactKind = Literal["matrix", "summary", "section", "figure", "code", "log"]
ProducedBy = Literal["librarian", "critic", "analyst", "scribe", "human"]


class Artifact(BaseModel):
    id: UUID
    project_id: UUID
    kind: ArtifactKind
    label: str
    content: str
    mime_type: str
    produced_by: ProducedBy
    parent_id: UUID | None = None
    created_at: datetime


class AuditLogEntry(BaseModel):
    id: UUID
    project_id: UUID
    workflow_run_id: UUID | None = None
    actor: Literal["system", "user", "librarian", "critic", "analyst", "scribe"]
    action: str
    payload: dict[str, object]
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    created_at: datetime
