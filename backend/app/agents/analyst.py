"""The Analyst agent — Phase 3 (Sandbox Compute). See SPEC.md §6.3.

The Analyst takes a user-provided task description and a set of uploaded
datasets and produces:

  1. A Python script that, when run, generates the figures + tables the
     user asked for. This is shown to the user *before* execution — the
     BRD §10 sandbox-escape mitigation requires explicit code approval.
  2. A short methods narrative the Scribe can pull into the methodology
     section of the manuscript.

Sprint 2 scope: LLM-only. The agent emits a `code` Artifact and a
`methods_narrative` string; sandbox execution lives in Sprint 3
(:mod:`app.services.sandbox`) and the wiring lives in Sprint 4
(:mod:`app.graph.workflow`). Until then, ``Analyst.run()`` returns the
proposed code without executing it — the second HITL gate is
``await_code_approval`` (BRD FR-1.4 / FR-2.3), the third is
``await_analysis_approval`` (post-execution review).

Failure handling mirrors the Critic:
  * The LLM call is wrapped — any provider failure produces a `log`
    Artifact instead of crashing.
  * A static AST denylist scan (`_validate_proposed_code`) rejects code
    that imports network, subprocess, or filesystem-escape modules
    BEFORE the sandbox ever runs (defense-in-depth on top of the Docker
    `--network=none` flag the sandbox enforces).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel

from app.agents._prompt_safety import SYSTEM_ANCHOR, safe_tag
from app.agents.base import Agent
from app.models.schemas import Artifact
from app.services.llm import LLMGateway, get_llm_gateway
from app.utils.logging import get_logger

_log = get_logger(__name__)


# Modules the static scanner rejects. The list is intentionally aggressive —
# Phase-3 code is supposed to be data analysis (pandas / numpy / matplotlib /
# scikit-learn / scipy), not arbitrary system access. A user who needs to
# import one of these can OVERRIDE the proposed code at the gate; the
# override is loud in the audit log.
_DENY_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "tempfile",
        "ctypes",
        "requests",
        "urllib",
        "urllib2",
        "urllib3",
        "httpx",
        "aiohttp",
        "pickle",
        "marshal",
        "pty",
        "asyncio",
        "multiprocessing",
        "threading",
        "platform",
        "pathlib",  # use io.BytesIO/StringIO and the bound /work mount
    }
)

# Allowed top-level modules — anything missing from this set still passes
# (denylist semantics) unless it's in _DENY_IMPORTS. We document the
# expected set so the static scanner can warn (not fail) on unfamiliar
# modules: the sandbox image only contains these, so an unknown import is
# a near-certain "will fail to run" signal.
_EXPECTED_MODULES: frozenset[str] = frozenset(
    {
        "numpy",
        "pandas",
        "matplotlib",
        "scipy",
        "sklearn",
        "json",
        "math",
        "statistics",
        "io",
        "csv",
        "itertools",
        "collections",
        "datetime",
        "re",
    }
)


@dataclass(frozen=True, slots=True)
class StaticScanResult:
    """Outcome of :func:`_validate_proposed_code`."""

    ok: bool
    denied: list[str]  # module names hit by _DENY_IMPORTS
    unknown: list[str]  # module names not in _EXPECTED_MODULES (warning only)
    error: str | None = None  # set when the source fails to parse


def _validate_proposed_code(source: str) -> StaticScanResult:
    """Static-AST scan over generated Python source.

    Hard-rejects any import (top-level or inside a function) of a denylisted
    module. Reports unknown-but-not-denied modules as warnings — those don't
    block the gate but do show up in the audit row so a reviewer can see
    the LLM is reaching for something the sandbox image won't ship.

    Returns :class:`StaticScanResult` rather than raising so the caller can
    decide whether to fail the run or surface the warnings to the user.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return StaticScanResult(
            ok=False, denied=[], unknown=[], error=f"SyntaxError: {exc.msg} (line {exc.lineno})"
        )

    denied: list[str] = []
    unknown: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _DENY_IMPORTS:
                    denied.append(root)
                elif root not in _EXPECTED_MODULES:
                    unknown.append(root)
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _DENY_IMPORTS:
                denied.append(module)
            elif module and module not in _EXPECTED_MODULES:
                unknown.append(module)

    return StaticScanResult(
        ok=not denied,
        denied=sorted(set(denied)),
        unknown=sorted(set(unknown)),
        error=None,
    )


# ---------------------------------------------------------------------------
# Pydantic I/O contract
# ---------------------------------------------------------------------------


class DatasetRef(BaseModel):
    """Minimal subset of a Dataset row passed to the Analyst.

    The Analyst only needs the schema and filename — the bytes are read by
    the sandbox at execution time (Sprint 3), not by the LLM. Keeping the
    LLM payload schema-only also keeps token cost bounded for large files.
    """

    id: UUID
    filename: str
    columns: list[str]
    rowcount: int


class AnalystInput(BaseModel):
    """Sprint 2 contract for the Analyst.

    `task_description` is the user's plain-English request ("plot the
    distribution of column X grouped by Y"). `feedback` is populated on
    regenerate-with-feedback (rejection path). `prior_code` is populated
    when the user has rejected a previous proposal and we want the LLM to
    revise rather than start from scratch.
    """

    project_id: UUID
    task_description: str
    datasets: list[DatasetRef]
    feedback: str | None = None
    prior_code: str | None = None


class AnalystProposal(BaseModel):
    """What the Analyst produces in Sprint 2 — code + methods narrative.

    Sprint 4 will introduce a second output type — `AnalystResult` —
    carrying the executed figures + log artifacts. This type is what the
    `await_code_approval` HITL gate displays to the user.
    """

    code: Artifact
    methods_narrative: str
    scan: StaticScanResult

    # `StaticScanResult` is a frozen dataclass, not a BaseModel — tell
    # Pydantic that's OK to round-trip.
    model_config = {"arbitrary_types_allowed": True}


class AnalystUsage(BaseModel):
    """Token + cost rollup for one Analyst proposal (BRD FR-3.3 / §4.3)."""

    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    llm_calls: int = 0


class AnalystOutput(BaseModel):
    """Top-level return type — proposal + usage."""

    proposal: AnalystProposal
    usage: AnalystUsage = AnalystUsage()


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_CODE_PROMPT_TEMPLATE = """\
You are a research data analyst. Generate a single Python script that
produces the analysis the user requested. The script will be executed in a
sandboxed environment with the following constraints — your code MUST work
within them:

  * No network access. Do not import `requests`, `urllib`, `httpx`, or
    any client library that reaches the internet.
  * No subprocess / shell access. Do not import `os`, `sys`, `subprocess`,
    or `shutil`.
  * Datasets are pre-loaded into a directory called `/work/datasets/`.
    Each dataset's filename is listed below; read with the standard
    library or pandas.
  * Save each figure to `/work/figures/figure_NN.png` (NN = zero-padded
    1-based index). matplotlib's `savefig` is the standard way.
  * Print any text output (counts, summary stats) to stdout. The sandbox
    captures the first 64 KiB.
  * The script runs once and exits — no interactive input, no while-True
    polling.

{feedback_block}{prior_block}User task: {task_block}

{datasets_block}

After the code, write a 2-3 sentence "methods narrative" the Scribe will
quote in the manuscript's methodology section. Use prose, no bullet
points. Return JSON in this exact shape:

  {{
    "code": "...python source as a single string...",
    "methods_narrative": "..."
  }}{system_anchor}"""


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


class _LLMResponse(BaseModel):
    """Object-root envelope for the structured LLM call."""

    code: str
    methods_narrative: str = ""


class Analyst(Agent[AnalystInput, AnalystOutput]):
    """Phase-3 sandboxed-compute persona (Sprint 2: LLM-only).

    Sprint 3 will introduce an :meth:`execute` method that ships the
    approved code into the sandbox; for now :meth:`run` only proposes the
    code and never runs it. The graph keeps :meth:`run` behind the
    ``await_code_approval`` interrupt (Sprint 4) so the BRD §10 invariant
    — "user reviews code before execution" — is enforceable end-to-end.
    """

    name = "analyst"

    def __init__(self, llm: LLMGateway | None = None) -> None:
        self._llm = llm if llm is not None else get_llm_gateway()

    async def run(self, payload: AnalystInput) -> AnalystOutput:
        usage = AnalystUsage(model=getattr(self._llm, "model_name", None))
        proposal = await self._propose(payload, usage)
        _log.info(
            "analyst_propose_done",
            project_id=str(payload.project_id),
            datasets=len(payload.datasets),
            scan_ok=proposal.scan.ok,
            denied=len(proposal.scan.denied),
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
        )
        return AnalystOutput(proposal=proposal, usage=usage)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _propose(self, payload: AnalystInput, usage: AnalystUsage) -> AnalystProposal:
        prompt = self._render_prompt(payload)

        try:
            from google.genai import types as genai_types

            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_LLMResponse,
            )
            text, telemetry = await self._llm.complete(prompt, config=config)
            self._accumulate(usage, telemetry)
            envelope = _LLMResponse.model_validate_json(text)
            code = envelope.code.strip() + "\n"
            narrative = envelope.methods_narrative.strip()
        except Exception as exc:  # LLM provider failure → surface gracefully
            _log.warning("analyst_proposal_failed", error_type=type(exc).__name__, error=str(exc))
            code = (
                "# Analyst LLM call failed. The reviewer can either retry "
                "(rejecting the gate) or supply replacement code via the "
                "override path.\n"
                f"# error: {type(exc).__name__}: {exc}\n"
            )
            narrative = "Code generation failed; no methods narrative produced."

        scan = _validate_proposed_code(code)
        now = datetime.now(tz=UTC)
        artifact = Artifact(
            id=uuid4(),
            project_id=payload.project_id,
            kind="code",
            label="analyst-proposal",
            content=code,
            mime_type="text/x-python",
            produced_by="analyst",
            created_at=now,
        )
        return AnalystProposal(code=artifact, methods_narrative=narrative, scan=scan)

    def _render_prompt(self, payload: AnalystInput) -> str:
        # W1-A1: every piece of user-supplied or LLM-supplied text gets
        # wrapped in an escaped XML tag so a hostile feedback string can't
        # override the system instructions above.
        task_block = safe_tag("task", payload.task_description)
        feedback_block = (
            f"Apply this revision: {safe_tag('reviewer_feedback', payload.feedback)}\n\n"
            if payload.feedback
            else ""
        )
        prior_block = (
            f"Revise the prior code rather than starting over:\n"
            f"{safe_tag('prior_code', payload.prior_code)}\n\n"
            if payload.prior_code
            else ""
        )

        if payload.datasets:
            dataset_lines: list[str] = []
            for ds in payload.datasets:
                dataset_lines.append(
                    safe_tag(
                        "dataset",
                        safe_tag("filename", ds.filename)
                        + safe_tag("rowcount", str(ds.rowcount))
                        + safe_tag("columns", ", ".join(ds.columns)),
                        attrs={"id": str(ds.id)},
                        raw=True,
                    )
                )
            datasets_block = "Datasets:\n" + "\n".join(dataset_lines)
        else:
            datasets_block = (
                "No datasets are attached. Generate code that demonstrates the "
                "requested analysis on a small synthetic example."
            )

        return _CODE_PROMPT_TEMPLATE.format(
            feedback_block=feedback_block,
            prior_block=prior_block,
            task_block=task_block,
            datasets_block=datasets_block,
            system_anchor=SYSTEM_ANCHOR,
        )

    @staticmethod
    def _accumulate(usage: AnalystUsage, telemetry: dict[str, object]) -> None:
        usage.llm_calls += 1
        tin = telemetry.get("tokens_in")
        if isinstance(tin, int):
            usage.tokens_in += tin
        tout = telemetry.get("tokens_out")
        if isinstance(tout, int):
            usage.tokens_out += tout
        cost = telemetry.get("cost_usd")
        if isinstance(cost, int | float):
            usage.cost_usd = (usage.cost_usd or 0.0) + float(cost)
        model = telemetry.get("model")
        if usage.model is None and isinstance(model, str):
            usage.model = model


# ---------------------------------------------------------------------------
# Public helpers (used by routes / graph)
# ---------------------------------------------------------------------------


def validate_user_override_code(source: str) -> StaticScanResult:
    """Re-run the static scan against user-supplied override code.

    The override path lets a user replace the LLM's proposal with their own
    code at the `await_code_approval` gate. We still scan it — the override
    is meant to be a last-resort surgical fix, not a wholesale bypass of
    the safety rails. A denied result raises a 422 at the route layer; a
    warning-only result (`unknown` non-empty, `denied` empty) is approved
    but recorded in the audit row.
    """
    return _validate_proposed_code(source)


def proposal_to_audit_payload(prop: AnalystProposal, *, sha256: str) -> dict[str, object]:
    """Serialise the proposal into the dict the audit-log row stores."""
    return {
        "artifact_id": str(prop.code.id),
        "code_sha256": sha256,
        "code_bytes": len(prop.code.content.encode()),
        "scan_ok": prop.scan.ok,
        "denied_imports": prop.scan.denied,
        "unknown_imports": prop.scan.unknown,
        "scan_error": prop.scan.error,
        "methods_narrative_chars": len(prop.methods_narrative),
    }


__all__ = [
    "Analyst",
    "AnalystInput",
    "AnalystOutput",
    "AnalystProposal",
    "AnalystUsage",
    "DatasetRef",
    "StaticScanResult",
    "proposal_to_audit_payload",
    "validate_user_override_code",
]
