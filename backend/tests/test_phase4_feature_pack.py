"""Phase-4 Feature Pack — Export Pack, Citation Manager v1, Telemetry.

Covers the BRD-mandated core shipped in this sprint:
  - Export Pack (FR-3.5): markdown / bibtex / package(zip) / bundle builders;
    manuscript-not-ready guard; bibtex restricted to the approved pool;
    package always carries disclosure + audit appendix with deterministic
    file names.
  - Citation Manager v1 (FR-1.5): citation resolution against the approved
    pool; unresolved-key detection; the `[@bad]`→`[@good]` correction helper.
  - Phase-4 Telemetry (NFR-6 / §9): the /usage drafting{} rollup counts the
    right audit rows and averages draft_ms.

All service-level (no HTTP layer) against the same in-memory SQLite session
fixture the other backend tests use.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import ArtifactRow, AuditLogRow, Base, PaperRow, ProjectRow, UserRow

TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-0000000000aa")


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session with all tables created + a seeded user/project."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with factory() as session:
        now = datetime.now(tz=UTC)
        session.add(
            UserRow(
                id=TEST_USER_ID,
                firebase_uid="test-firebase-uid",
                email="test@example.com",
                created_at=now,
            )
        )
        session.add(
            ProjectRow(
                id=TEST_PROJECT_ID,
                owner_id=TEST_USER_ID,
                title="Attention Is All You Need — A Review",
                seed_query="transformers",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _paper(
    key: str,
    *,
    approved: bool = True,
    source: str = "arxiv",
    pdf_url: str | None = None,
    year: int | None = 2020,
) -> PaperRow:
    return PaperRow(
        id=uuid4(),
        project_id=TEST_PROJECT_ID,
        source=source,
        external_id=f"{source}:{key}",
        title=f"Title {key}",
        authors=["Doe, J", "Roe, R"],
        year=year,
        abstract="abstract",
        pdf_url=pdf_url,
        citation_key=key,
        citation_count=1,
        approved=approved,
        added_at=datetime.now(tz=UTC),
    )


def _artifact(kind: str, label: str, content: str, produced_by: str = "scribe") -> ArtifactRow:
    return ArtifactRow(
        id=uuid4(),
        project_id=TEST_PROJECT_ID,
        kind=kind,
        label=label,
        content=content,
        mime_type="text/markdown",
        produced_by=produced_by,
        parent_id=None,
        created_at=datetime.now(tz=UTC),
    )


def _audit(
    action: str,
    *,
    payload: dict[str, object] | None = None,
    actor: str = "user",
    project_id: UUID = TEST_PROJECT_ID,
    created_at: datetime | None = None,
) -> AuditLogRow:
    return AuditLogRow(
        id=uuid4(),
        project_id=project_id,
        workflow_run_id=None,
        actor=actor,
        action=action,
        payload=payload or {},
        model=None,
        tokens_in=None,
        tokens_out=None,
        cost_usd=None,
        created_at=created_at or datetime.now(tz=UTC),
    )


# ===========================================================================
# Export Pack (FR-3.5)
# ===========================================================================


@pytest.mark.asyncio
async def test_export_markdown_returns_manuscript(db_session: AsyncSession) -> None:
    from app.services import export as ex

    db_session.add(_artifact("manuscript", "manuscript", "# The Manuscript\n\nBody."))
    await db_session.flush()

    out = await ex.build_manuscript_markdown(db_session, TEST_PROJECT_ID)
    assert out == "# The Manuscript\n\nBody."


@pytest.mark.asyncio
async def test_export_raises_when_no_manuscript(db_session: AsyncSession) -> None:
    from app.services import export as ex

    with pytest.raises(ex.ManuscriptNotReadyError):
        await ex.build_manuscript_markdown(db_session, TEST_PROJECT_ID)


@pytest.mark.asyncio
async def test_bibtex_contains_only_approved_pool(db_session: AsyncSession) -> None:
    from app.services import export as ex

    db_session.add(_paper("lecun2015", approved=True))
    db_session.add(_paper("smith2099", approved=False))  # NOT approved → excluded
    await db_session.flush()

    bib = await ex.build_bibtex(db_session, TEST_PROJECT_ID)
    assert "@article{lecun2015," in bib
    assert "smith2099" not in bib  # FR-2.4 invariant: cite only from the pool


@pytest.mark.asyncio
async def test_bibtex_empty_pool_is_safe(db_session: AsyncSession) -> None:
    from app.services import export as ex

    bib = await ex.build_bibtex(db_session, TEST_PROJECT_ID)
    assert "No approved papers" in bib


@pytest.mark.asyncio
async def test_package_zip_has_all_four_files_and_disclosure(db_session: AsyncSession) -> None:
    from app.services import export as ex

    db_session.add(_artifact("manuscript", "manuscript", "# M\n\n[@lecun2015]"))
    db_session.add(_paper("lecun2015", approved=True))
    db_session.add(_audit("user.approve", payload={"by": "u"}))
    await db_session.flush()

    data = await ex.build_package_zip(db_session, TEST_PROJECT_ID, "My Title")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        # Deterministic <slug>/ file naming.
        assert "my-title/manuscript.md" in names
        assert "my-title/references.bib" in names
        assert "my-title/ai-disclosure.md" in names
        assert "my-title/audit-appendix.md" in names
        disclosure = zf.read("my-title/ai-disclosure.md").decode()
        assert "disclosure" in disclosure.lower()
        appendix = zf.read("my-title/audit-appendix.md").decode()
        assert "user.approve" in appendix


@pytest.mark.asyncio
async def test_package_zip_requires_manuscript(db_session: AsyncSession) -> None:
    from app.services import export as ex

    with pytest.raises(ex.ManuscriptNotReadyError):
        await ex.build_package_zip(db_session, TEST_PROJECT_ID, "x")


@pytest.mark.asyncio
async def test_bundle_markdown_is_single_file_with_all_parts(db_session: AsyncSession) -> None:
    from app.services import export as ex

    db_session.add(_artifact("manuscript", "manuscript", "# Manuscript Body"))
    db_session.add(_paper("lecun2015", approved=True))
    db_session.add(_audit("user.override", payload={}))
    await db_session.flush()

    bundle = await ex.build_bundle_markdown(db_session, TEST_PROJECT_ID)
    assert "disclosure" in bundle.lower()
    assert "# Manuscript Body" in bundle
    assert "lecun2015" in bundle  # references embedded
    assert "audit" in bundle.lower()


# ===========================================================================
# Citation Manager v1 (FR-1.5)
# ===========================================================================


@pytest.mark.asyncio
async def test_citations_for_content_resolves_and_flags_unresolved() -> None:
    from app.services.citations import citations_for_content

    pool = [_paper("lecun2015", approved=True)]
    content = "Intro [@lecun2015] and a bad one [@ghost2099]."
    out = citations_for_content(content, pool)

    assert set(out["cited_keys"]) == {"lecun2015", "ghost2099"}
    assert out["unresolved_keys"] == ["ghost2099"]
    assert len(out["resolved"]) == 1
    assert out["resolved"][0]["citation_key"] == "lecun2015"


@pytest.mark.asyncio
async def test_resolve_section_citations_reads_latest_draft(db_session: AsyncSession) -> None:
    from app.services.citations import resolve_section_citations

    db_session.add(_paper("lecun2015", approved=True))
    db_session.add(_artifact("section", "introduction", "Body [@lecun2015] [@ghost2099]"))
    await db_session.flush()

    out = await resolve_section_citations(db_session, TEST_PROJECT_ID, "introduction")
    assert out["section"] == "introduction"
    assert out["unresolved_keys"] == ["ghost2099"]


@pytest.mark.asyncio
async def test_latest_section_unresolved_blocks_when_offending(db_session: AsyncSession) -> None:
    from app.services.citations import latest_section_unresolved

    db_session.add(_paper("lecun2015", approved=True))
    db_session.add(_artifact("section", "introduction", "Body [@ghost2099]"))
    await db_session.flush()

    unresolved = await latest_section_unresolved(db_session, TEST_PROJECT_ID)
    assert unresolved == ["ghost2099"]


@pytest.mark.asyncio
async def test_latest_section_unresolved_empty_when_clean(db_session: AsyncSession) -> None:
    from app.services.citations import latest_section_unresolved

    db_session.add(_paper("lecun2015", approved=True))
    db_session.add(_artifact("section", "introduction", "Body [@lecun2015]"))
    await db_session.flush()

    assert await latest_section_unresolved(db_session, TEST_PROJECT_ID) == []


def test_apply_citation_corrections_rewrites_only_markers() -> None:
    from app.services.citations import apply_citation_corrections

    content = "See [@ghost2099]. The word ghost2099 stays as prose."
    out = apply_citation_corrections(content, {"ghost2099": "lecun2015"})
    assert "[@lecun2015]" in out
    assert "[@ghost2099]" not in out
    # Plain-text occurrence untouched — we only rewrite the [@...] token.
    assert "word ghost2099 stays" in out


@pytest.mark.asyncio
async def test_approved_citation_keys_returns_pool_membership(
    db_session: AsyncSession,
) -> None:
    """W1-A2 helper: approved_citation_keys returns the project's approved set."""
    from app.services.citations import approved_citation_keys

    db_session.add(_paper("lecun2015", approved=True))
    db_session.add(_paper("bengio2003", approved=True))
    db_session.add(_paper("not_approved", approved=False))
    await db_session.flush()

    keys = await approved_citation_keys(db_session, TEST_PROJECT_ID)
    assert keys == {"lecun2015", "bengio2003"}


# ===========================================================================
# Phase-4 Telemetry (NFR-6 / §9)
# ===========================================================================


@pytest.mark.asyncio
async def test_drafting_telemetry_counts_and_averages(db_session: AsyncSession) -> None:
    from app.services.workflow import drafting_telemetry

    db_session.add(
        _audit("phase_4.section_ready", payload={"section": "abstract", "draft_ms": 1000})
    )
    db_session.add(
        _audit("phase_4.section_ready", payload={"section": "introduction", "draft_ms": 3000})
    )
    # A drafting regeneration (counted) + a discovery reject (NOT counted).
    db_session.add(_audit("user.reject", payload={"phase": "drafting"}))
    db_session.add(_audit("user.reject", payload={"phase": "discovery"}))
    db_session.add(_audit("user.override", payload={}))
    db_session.add(_audit("user.citation_correction", payload={"corrections": {"a": "b"}}))
    # Noise that must be ignored.
    db_session.add(_audit("agent.invoke", payload={}))
    await db_session.flush()

    t = await drafting_telemetry(db_session, TEST_PROJECT_ID)
    assert t["sections_drafted"] == 2
    assert t["regenerations"] == 1  # only the drafting-phase reject
    assert t["overrides"] == 1
    assert t["citation_corrections"] == 1
    assert t["avg_section_ms"] == 2000  # (1000 + 3000) / 2


@pytest.mark.asyncio
async def test_drafting_telemetry_empty_project(db_session: AsyncSession) -> None:
    from app.services.workflow import drafting_telemetry

    t = await drafting_telemetry(db_session, TEST_PROJECT_ID)
    assert t == {
        "sections_drafted": 0,
        "regenerations": 0,
        "overrides": 0,
        "citation_corrections": 0,
        "avg_section_ms": None,
    }


@pytest.mark.asyncio
async def test_drafting_telemetry_avg_ignores_missing_draft_ms(db_session: AsyncSession) -> None:
    from app.services.workflow import drafting_telemetry

    # One section_ready has no draft_ms (older row) — avg must use only the
    # row that does, not crash or count it as zero.
    db_session.add(_audit("phase_4.section_ready", payload={"section": "abstract"}))
    db_session.add(_audit("phase_4.section_ready", payload={"section": "intro", "draft_ms": 500}))
    await db_session.flush()

    t = await drafting_telemetry(db_session, TEST_PROJECT_ID)
    assert t["sections_drafted"] == 2
    assert t["avg_section_ms"] == 500


@pytest.mark.asyncio
async def test_drafting_telemetry_scopes_to_project(db_session: AsyncSession) -> None:
    """Telemetry must not bleed across projects — rows for another project_id
    are excluded from this project's rollup."""
    from app.services.workflow import drafting_telemetry

    db_session.add(_audit("user.override", payload={}, project_id=uuid4()))  # other project
    db_session.add(_audit("user.override", payload={}))
    await db_session.flush()

    t = await drafting_telemetry(db_session, TEST_PROJECT_ID)
    assert t["overrides"] == 1  # only this project's row


# ===========================================================================
# W2-S1 — server-enforced override_reason when force_unresolved=true
# ===========================================================================


def test_approve_payload_rejects_force_without_reason() -> None:
    """W2-S1: force_unresolved=true requires a non-empty override_reason at
    the schema level. A curl-style bypass of the frontend disable cannot
    leave the audit log with an empty-reason forced approval."""
    import pytest as _pytest
    from pydantic import ValidationError

    from app.api.routes.workflow import ApprovePayload

    # Missing reason entirely → ValidationError.
    with _pytest.raises(ValidationError, match="override_reason is required"):
        ApprovePayload(force_unresolved=True)

    # Whitespace-only reason → ValidationError (strip then check non-empty).
    with _pytest.raises(ValidationError, match="override_reason is required"):
        ApprovePayload(force_unresolved=True, override_reason="   \n\t  ")

    # Empty string → ValidationError.
    with _pytest.raises(ValidationError, match="override_reason is required"):
        ApprovePayload(force_unresolved=True, override_reason="")


def test_approve_payload_accepts_force_with_reason() -> None:
    """Happy path: real reason text passes validation."""
    from app.api.routes.workflow import ApprovePayload

    p = ApprovePayload(force_unresolved=True, override_reason="intentional placeholder")
    assert p.force_unresolved is True
    assert p.override_reason == "intentional placeholder"


def test_approve_payload_no_force_no_reason_required() -> None:
    """Regression guard: when force_unresolved=False (default), an empty
    override_reason is fine — the new validator only kicks in on force."""
    from app.api.routes.workflow import ApprovePayload

    p = ApprovePayload()
    assert p.force_unresolved is False
    assert p.override_reason is None
