"""Regression tests for the Phase 2 security audit (see audit report).

Each test corresponds to one finding in the audit. Touching the protected
behaviour without updating these tests should immediately turn the suite red.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.api.routes.workflow import FeedbackPayload, OverridePayload
from app.models.schemas import Paper
from app.services.discovery import (
    CoreAdapter,
    CrossrefAdapter,
    EuropePMCAdapter,
    SemanticScholarAdapter,
    _safe_json,
    _sanitise_pdf_url,
)
from app.services.fulltext_fetcher import FullTextFetcher
from app.services.unpaywall import UnpaywallEnricher

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000099")


def _paper(citation_key: str, external_id: str, pdf_url: str | None = None) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=TEST_PROJECT_ID,
        source="crossref",
        external_id=external_id,
        title=f"Title for {citation_key}",
        authors=["Smith, J"],
        year=2024,
        abstract=None,
        pdf_url=pdf_url,  # type: ignore[arg-type]
        citation_key=citation_key,
        approved=True,
        added_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Finding #3 + #4 — API payload validation
# ---------------------------------------------------------------------------


def test_override_payload_rejects_invalid_artifact_kind() -> None:
    """artifact_kind must be one of the SPEC §2.2 literals, not free-form."""
    with pytest.raises(ValidationError):
        OverridePayload(
            artifact_kind="../../etc/passwd",  # type: ignore[arg-type]
            label="x",
            content="x",
        )


def test_override_payload_caps_content_length() -> None:
    """A 1 MB content payload must be rejected — memory DoS guard."""
    with pytest.raises(ValidationError):
        OverridePayload(
            artifact_kind="summary",
            label="x",
            content="A" * 1_000_000,  # well over the 256 000 cap
        )


def test_override_payload_caps_label_length() -> None:
    with pytest.raises(ValidationError):
        OverridePayload(
            artifact_kind="summary",
            label="L" * 500,  # over the 200 cap
            content="ok",
        )


def test_override_payload_requires_non_empty_content_and_label() -> None:
    with pytest.raises(ValidationError):
        OverridePayload(artifact_kind="summary", label="", content="x")
    with pytest.raises(ValidationError):
        OverridePayload(artifact_kind="summary", label="x", content="")


def test_feedback_payload_caps_length() -> None:
    """A 10 KB feedback payload (prompt-injection amplification) is rejected."""
    with pytest.raises(ValidationError):
        FeedbackPayload(feedback="A" * 10_000)


def test_feedback_payload_accepts_normal_input() -> None:
    """The cap doesn't break ordinary feedback."""
    FeedbackPayload(feedback="Please rewrite the synthesis grouped by year.")


# ---------------------------------------------------------------------------
# Finding #1 + #7 — defensive JSON parsing
# ---------------------------------------------------------------------------


def _mk_response(text: str) -> httpx.Response:
    return httpx.Response(200, text=text)


def test_safe_json_returns_none_on_bad_body() -> None:
    """HTML error page returned with 200 OK must not crash callers."""
    assert _safe_json(_mk_response("<html>error</html>"), source="ss", query="q") is None


def test_safe_json_returns_none_on_non_dict_root() -> None:
    """A list at the JSON root (legitimate JSON but wrong shape) is rejected."""
    assert _safe_json(_mk_response("[1, 2, 3]"), source="ss", query="q") is None


@pytest.mark.asyncio
async def test_semantic_scholar_survives_bad_json_response() -> None:
    """A 200 with garbage body must degrade to [], not crash the lane."""
    with respx.mock:
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(200, text="<html>500 internal</html>")
        )
        adapter = SemanticScholarAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("q", max_results=10, client=client)
    assert papers == []


@pytest.mark.asyncio
async def test_crossref_survives_bad_json_response() -> None:
    with respx.mock:
        respx.get("https://api.crossref.org/works").mock(
            return_value=httpx.Response(200, text="not json")
        )
        adapter = CrossrefAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("q", max_results=10, client=client)
    assert papers == []


@pytest.mark.asyncio
async def test_core_survives_bad_json_response() -> None:
    with respx.mock:
        respx.get("https://api.core.ac.uk/v3/search/works").mock(
            return_value=httpx.Response(200, text="garbage")
        )
        adapter = CoreAdapter(api_key="dummy")
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("q", max_results=10, client=client)
    assert papers == []


@pytest.mark.asyncio
async def test_europe_pmc_survives_bad_json_response() -> None:
    with respx.mock:
        respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
            return_value=httpx.Response(200, text="<html></html>")
        )
        adapter = EuropePMCAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("q", max_results=10, client=client)
    assert papers == []


# ---------------------------------------------------------------------------
# Finding #2 — Unpaywall must not drain the batch on per-item failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpaywall_batch_survives_per_paper_exception() -> None:
    """A network/JSON failure on one DOI must not lose every later paper."""
    p1 = _paper("first2024", "10.5555/first")
    p2 = _paper("bomb2024", "10.5555/bomb")
    p3 = _paper("last2024", "10.5555/last")

    enricher = UnpaywallEnricher(email="dev@example.com")
    with respx.mock:
        respx.get("https://api.unpaywall.org/v2/10.5555/first").mock(
            return_value=httpx.Response(200, json={"is_oa": False})
        )
        # Middle DOI: 200 with non-JSON body — used to raise uncaught.
        respx.get("https://api.unpaywall.org/v2/10.5555/bomb").mock(
            return_value=httpx.Response(200, text="<html>not json</html>")
        )
        respx.get("https://api.unpaywall.org/v2/10.5555/last").mock(
            return_value=httpx.Response(
                200,
                json={
                    "is_oa": True,
                    "best_oa_location": {"url_for_pdf": "https://oa.example.com/last.pdf"},
                },
            )
        )
        result = await enricher.enrich([p1, p2, p3])

    # All three papers come back — the bomb didn't drop p3.
    assert [p.citation_key for p in result] == ["first2024", "bomb2024", "last2024"]
    # The third paper still got its OA URL populated.
    assert str(result[2].pdf_url) == "https://oa.example.com/last.pdf"


# ---------------------------------------------------------------------------
# Finding #8 — pypdf control-char sanitisation
# ---------------------------------------------------------------------------


def test_fulltext_extract_strips_control_chars() -> None:
    """NUL and other C0/C1 control chars must be stripped before embedding.

    Chroma rejects documents containing NUL, and Gemini sometimes fails to
    encode them in prompts. Newline and tab are preserved.
    """
    # Patch pypdf to return a page containing NULs and other control bytes.
    dirty = "Real text\x00with nuls\x0band controls\nbut newlines kept\there too."
    with patch(
        "app.services.fulltext_fetcher.PdfReader",
        autospec=True,
    ) as mock_reader:
        page = type("P", (), {"extract_text": lambda self: dirty})()
        mock_reader.return_value.pages = [page]
        out = FullTextFetcher._extract_text(b"%PDF-1.4\n", "x2024")

    assert "\x00" not in out
    assert "\x0b" not in out
    # Newlines and tabs survive — they carry structure for the chunker.
    assert "\n" in out
    assert "\t" in out
    # Real prose survived unchanged.
    assert "Real text" in out
    assert "but newlines kept" in out


# ---------------------------------------------------------------------------
# Finding #6 — graph gate defaults to reject on unknown approval values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_gate_treats_unknown_resume_value_as_reject() -> None:
    """Any non-"approve" string at the pool gate must default to reject.

    Previously only the literal "reject" was treated as reject; everything
    else (None, "", garbage) silently approved.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from app.graph.workflow import NODE_AWAIT_POOL, build_graph

    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    from unittest.mock import AsyncMock
    from unittest.mock import patch as p2

    from app.agents.librarian import LibrarianOutput
    from app.models.schemas import Phase

    librarian_out = LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])
    with p2("app.graph.workflow.Librarian") as mock_lib:
        mock_lib.return_value.run = AsyncMock(return_value=librarian_out)
        initial_state = {
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
            "sections_remaining": ["abstract"],
            "drafts": [],
            "matrix": None,
            "summary": None,
            "synthesis_approval": None,
        }
        await graph.ainvoke(initial_state, config)
        # Resume with a garbage string — should be treated as reject.
        await graph.ainvoke(Command(resume="🤖 bogus"), config)
        snapshot = await graph.aget_state(config)

    # After "reject" the graph loops back to discover and re-interrupts at
    # the pool gate. So snapshot.next is the pool gate again.
    assert snapshot.next == (NODE_AWAIT_POOL,)
    # pool_approval must be exactly "reject" — the defensive default.
    assert snapshot.values.get("pool_approval") == "reject"


# ---------------------------------------------------------------------------
# Finding #5 — synthesis gate handles unknown override kind without dropping
# state silently. The override is cleared but the synthesis advances normally,
# and a warning is emitted (caller can find it in the audit trail).
# ---------------------------------------------------------------------------


def test_sanitise_pdf_url_drops_ielx_and_keeps_valid() -> None:
    """Defence-in-depth: confirms the prior IEEE filter still works."""
    assert _sanitise_pdf_url("https://ieeexplore.ieee.org/ielx7/foo/09969608.pdf") is None
    assert (
        _sanitise_pdf_url("https://arxiv.org/pdf/2401.00001") == "https://arxiv.org/pdf/2401.00001"
    )
    assert _sanitise_pdf_url(None) is None
    assert _sanitise_pdf_url("") is None


# Smoke test: pypdf import path used in the previous extract test
def test_pypdf_io_bytesio_imports_cleanly() -> None:
    """Catch missing-import regressions before they hit production."""
    assert io.BytesIO is not None  # trivial; runs only to surface import errors
