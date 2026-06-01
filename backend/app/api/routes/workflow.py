"""Workflow control routes. See SPEC.md §3.3."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import CurrentUser, DbSession
from app.api.rate_limit import rate_limit
from app.models.db import ProjectRow
from app.models.schemas import WorkflowRun
from app.services import workflow as wf_svc

router = APIRouter(tags=["workflow"], prefix="/projects/{project_id}/workflow")


# Per the security audit (see docs/audit/phase-2-audit.md):
# - feedback flows into LLM prompts; cap length to prevent prompt-injection
#   amplification and to keep us under the model's context window.
# - override content lands in the artifacts table and into LangGraph state;
#   cap to a sane reviewable size (~256 KB of markdown is plenty).
# - artifact_kind and mime_type are constrained to the SPEC §2.2 literals so
#   a crafted client cannot write garbage strings into the DB.

_MAX_FEEDBACK_CHARS = 2_000
_MAX_OVERRIDE_CONTENT_CHARS = 256_000
_MAX_LABEL_CHARS = 200
_MAX_MIME_CHARS = 100

# Artifact.kind literal from SPEC §2.2 — replicated here so the route can
# reject unknown values at the API boundary before they reach the DB.
ArtifactKindIn = Literal["matrix", "summary", "section", "figure", "code", "log"]


class FeedbackPayload(BaseModel):
    feedback: str | None = Field(default=None, max_length=_MAX_FEEDBACK_CHARS)


class ApprovePayload(BaseModel):
    """Approve payload. `feedback` carries optional reviewer notes; the two new
    fields (FR-1.5) let a reviewer knowingly approve a section that still has
    unresolved citations — an audited escape hatch from the default block."""

    feedback: str | None = Field(default=None, max_length=_MAX_FEEDBACK_CHARS)
    # Approve despite unresolved citation keys (default: block). Additive.
    force_unresolved: bool = False
    # When force_unresolved is True this MUST be non-empty (W2-S1) — the
    # frontend already disables the button on empty input, but the server
    # also enforces so a curl call can't bypass it and leave the audit log
    # with empty-reason forced approvals.
    override_reason: str | None = Field(default=None, max_length=_MAX_FEEDBACK_CHARS)

    @model_validator(mode="after")
    def _require_reason_on_force(self) -> ApprovePayload:
        if self.force_unresolved and not (self.override_reason or "").strip():
            raise ValueError("override_reason is required (non-empty) when force_unresolved=true")
        return self


class OverridePayload(BaseModel):
    artifact_kind: ArtifactKindIn
    label: str = Field(..., min_length=1, max_length=_MAX_LABEL_CHARS)
    content: str = Field(..., min_length=1, max_length=_MAX_OVERRIDE_CONTENT_CHARS)
    mime_type: str = Field(default="text/markdown", max_length=_MAX_MIME_CHARS)
    # FR-1.5: replace malformed citation keys before approving. Optional/additive.
    # Map of {bad_key: approved_key}; applied to `content` and audited as a
    # human citation correction.
    citation_corrections: dict[str, str] | None = None
    # Free-text rationale for the manual edit (audited).
    override_reason: str | None = Field(default=None, max_length=_MAX_FEEDBACK_CHARS)


@router.post(
    "/start",
    response_model=WorkflowRun,
    # M1-C: workflow starts kick off background Librarian/Critic agent
    # calls. Cap to 10/min/user so a runaway client can't soak Gemini
    # quota or saturate the asyncio task pool.
    dependencies=[Depends(rate_limit("workflow.start", max_per_window=10))],
)
async def start_workflow(project_id: UUID, user: CurrentUser, db: DbSession) -> WorkflowRun:
    """Start or resume the workflow for a project."""
    await _assert_project_owned(db, project_id, user.id)
    return await wf_svc.start_workflow(db, project_id, user.id)


@router.get("", response_model=WorkflowRun)
async def get_workflow(project_id: UUID, user: CurrentUser, db: DbSession) -> WorkflowRun:
    """Get the active workflow run for a project."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    return wf_svc._run_to_schema(run)


@router.post(
    "/approve",
    response_model=WorkflowRun,
    # W2-S2: cap per-user approve spam — even a successful approve writes
    # audit rows and triggers a graph resume. 30/min is well above any
    # human reviewer cadence (one approve every 2s sustained) but stops
    # a runaway client.
    dependencies=[Depends(rate_limit("workflow.approve", max_per_window=30))],
)
async def approve(
    project_id: UUID, payload: ApprovePayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Approve the pending phase and advance the workflow (SPEC.md §7).

    FR-1.5 citation guard: when approving a *drafting* section, if the current
    draft cites keys not in the approved pool, the approve is BLOCKED (409
    unresolved_citations) unless the caller sets force_unresolved=true with an
    override_reason — which is recorded for audit.
    """
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if run.state != "awaiting_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "phase_locked", "message": "Workflow is not awaiting approval."},
        )

    # Citation guard — only meaningful in the drafting phase (sections cite).
    # Checks the most-recent section draft (the one awaiting this approval).
    if run.phase == "drafting":
        from app.services.citations import latest_section_unresolved

        unresolved = await latest_section_unresolved(db, project_id)
        if unresolved and not payload.force_unresolved:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "unresolved_citations",
                    "message": (
                        "This section cites keys not in the approved pool. Fix them, "
                        "or approve with force_unresolved + override_reason."
                    ),
                    "keys": unresolved,
                },
            )

    return await wf_svc.approve_workflow(
        db,
        project_id,
        run.id,
        user.id,
        payload.feedback,
        forced_unresolved=payload.force_unresolved,
        override_reason=payload.override_reason,
    )


@router.post(
    "/reject",
    response_model=WorkflowRun,
    # W2-S2: same posture as approve. Reject triggers an agent regenerate
    # (LLM call → tokens, money). 30/min/user.
    dependencies=[Depends(rate_limit("workflow.reject", max_per_window=30))],
)
async def reject(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Reject the current phase output and re-run with feedback."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if run.state != "awaiting_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "phase_locked", "message": "Workflow is not awaiting approval."},
        )
    feedback = payload.feedback or "Please refine the output."
    return await wf_svc.reject_workflow(db, project_id, run.id, user.id, feedback)


@router.post(
    "/override",
    response_model=WorkflowRun,
    # W2-S2: override writes up to 256 KB into ArtifactRow + 1 audit row.
    # 20/min is the tightest of the three (DB write blast radius is the
    # largest of the gate endpoints).
    dependencies=[Depends(rate_limit("workflow.override", max_per_window=20))],
)
async def override(
    project_id: UUID, payload: OverridePayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Submit a manually-edited artifact in place of the agent output.

    Writes an ArtifactRow (produced_by='human') and an audit entry
    before advancing the gate (SPEC §7.3).
    """
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if run.state != "awaiting_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "phase_locked", "message": "Workflow is not awaiting approval."},
        )

    # FR-1.5 citation correction: replace any malformed `[@bad]` markers with the
    # reviewer-chosen valid `[@good]` keys, then record the human edit for audit.
    # W1-A2: replacement keys MUST be in the approved pool — otherwise we'd
    # rewrite one hallucinated key to another and lie about it in the audit log.
    content = payload.content
    if payload.citation_corrections:
        from app.services.citations import (
            apply_citation_corrections,
            approved_citation_keys,
        )

        approved = await approved_citation_keys(db, project_id)
        replacements = set(payload.citation_corrections.values())
        bad_replacements = sorted(replacements - approved)
        if bad_replacements:
            raise HTTPException(
                # FastAPI deprecated _ENTITY in favor of _CONTENT; same 422.
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_citation_correction",
                    "message": ("citation_corrections target keys must be in the approved pool"),
                    "bad_replacements": bad_replacements,
                },
            )

        content = apply_citation_corrections(content, payload.citation_corrections)
        await wf_svc.record_citation_correction(
            db,
            project_id=project_id,
            run_id=run.id,
            user_id=user.id,
            label=payload.label,
            corrections=payload.citation_corrections,
            reason=payload.override_reason,
        )

    return await wf_svc.override_workflow(
        db,
        project_id=project_id,
        run_id=run.id,
        user_id=user.id,
        artifact_kind=payload.artifact_kind,
        label=payload.label,
        content=content,
        mime_type=payload.mime_type,
    )


# ---------------------------------------------------------------------------
# Phase 3 — analysis gates (SPEC v0.3 §3.3). Dedicated endpoints (rather than
# overloading /approve|/reject) keep the audit trail unambiguous and give the
# code-approval path a place to validate override_code with the AST denylist
# BEFORE we resume the graph.
# ---------------------------------------------------------------------------


class ApproveCodePayload(BaseModel):
    """Approve the Phase-3 code proposal.

    `override_code` is the optional user-edited code that replaces the LLM's
    proposal. It's scanned for denied imports here before the graph resumes;
    failing scan rejects with 422 (`code_static_scan_failed`).
    """

    feedback: str | None = Field(default=None, max_length=_MAX_FEEDBACK_CHARS)
    override_code: str | None = Field(default=None, max_length=_MAX_OVERRIDE_CONTENT_CHARS)


@router.post(
    "/analysis/approve-code",
    response_model=WorkflowRun,
    dependencies=[Depends(rate_limit("workflow.approve_code", max_per_window=30))],
)
async def approve_analysis_code(
    project_id: UUID, payload: ApproveCodePayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Approve (or override) the Analyst's proposed code, then run the sandbox."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")

    if payload.override_code:
        # The same AST denylist that gates the LLM proposal also gates the
        # user override. Defense in depth — the Docker container still
        # enforces --network=none etc., but rejecting here keeps the audit
        # log clean and surfaces the problem to the user immediately.
        from app.agents.analyst import validate_user_override_code

        scan = validate_user_override_code(payload.override_code)
        if not scan.ok:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "code_static_scan_failed",
                    "message": "Override code uses a denied or invalid import.",
                    "denied": scan.denied,
                    "error": scan.error,
                },
            )

    return await wf_svc.approve_code_workflow(
        db,
        project_id=project_id,
        run_id=run.id,
        user_id=user.id,
        feedback=payload.feedback,
        override_code=payload.override_code,
    )


@router.post(
    "/analysis/reject-code",
    response_model=WorkflowRun,
    dependencies=[Depends(rate_limit("workflow.reject_code", max_per_window=30))],
)
async def reject_analysis_code(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Reject the proposed code; the graph re-runs analyze_propose with feedback."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if not payload.feedback:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "feedback_required",
                "message": "Feedback is required when rejecting analyst code.",
            },
        )
    return await wf_svc.reject_code_workflow(
        db,
        project_id=project_id,
        run_id=run.id,
        user_id=user.id,
        feedback=payload.feedback,
    )


@router.post(
    "/analysis/approve-results",
    response_model=WorkflowRun,
    dependencies=[Depends(rate_limit("workflow.approve_results", max_per_window=30))],
)
async def approve_analysis_results(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Approve the executed results; graph advances to drafting."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    return await wf_svc.approve_results_workflow(
        db,
        project_id=project_id,
        run_id=run.id,
        user_id=user.id,
        feedback=payload.feedback,
    )


@router.post(
    "/analysis/reject-results",
    response_model=WorkflowRun,
    dependencies=[Depends(rate_limit("workflow.reject_results", max_per_window=30))],
)
async def reject_analysis_results(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Reject the executed results; graph re-runs analyze_propose."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if not payload.feedback:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "feedback_required",
                "message": "Feedback is required when rejecting analyst results.",
            },
        )
    return await wf_svc.reject_results_workflow(
        db,
        project_id=project_id,
        run_id=run.id,
        user_id=user.id,
        feedback=payload.feedback,
    )


# NOTE: there is deliberately no GET /workflow/candidates endpoint.
# The candidate/approved pool has a single source of truth — the `papers`
# DB table, read via GET /projects/{id}/papers (SPEC §3.4) and toggled via
# PATCH /papers/{id}. An earlier GET /workflow/candidates read the LangGraph
# checkpoint directly, which could diverge from the DB (PR #5 finding: the
# checkpoint and the papers table are two sources of truth). _run_graph and
# the Phase-1 re-pause branch of _resume_graph both persist candidates into
# the papers table at every pool gate, so the DB is always authoritative.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_project_owned(db: DbSession, project_id: UUID, user_id: UUID) -> None:
    row = await db.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project {project_id} not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")
