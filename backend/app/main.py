"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import artifacts, health, papers, projects, websocket, workflow
from app.config import get_settings
from app.utils.logging import configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    import structlog
    from sqlalchemy import update

    from app.db.session import get_session
    from app.graph.workflow import create_postgres_checkpointer
    from app.models.db import WorkflowRunRow
    from app.services.workflow import init_graph

    _log = structlog.get_logger(__name__)

    async def _cleanup_orphaned_runs() -> None:
        try:
            async with get_session() as session:
                await session.execute(
                    update(WorkflowRunRow)
                    .where(WorkflowRunRow.state == "running")
                    .values(state="failed")
                )
            _log.info("cleaned_up_orphaned_workflow_runs")
        except Exception as exc:
            _log.warning("failed_to_cleanup_orphaned_runs", error=str(exc))

    # Run cleanup before graph initialization
    await _cleanup_orphaned_runs()

    checkpointer = None

    try:
        checkpointer = await create_postgres_checkpointer(settings.database_url)
        await checkpointer.setup()  # creates langgraph_checkpoints table if absent
        _log.info("checkpointer_postgres_ready")
    except Exception as exc:
        # Postgres unavailable (no Docker in dev) — fall back to in-memory checkpointer.
        # WARNING: state is lost on restart. Never use in production.
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
        _log.warning(
            "checkpointer_fallback_memory",
            reason=str(exc),
            hint="Start Docker (docker compose up postgres) for persistent state.",
        )

    await init_graph(checkpointer)

    yield

    if hasattr(checkpointer, "aclose"):
        await checkpointer.aclose()
    from app.db.session import dispose_engine

    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ResearchFlow AI — Remote Engine",
        version=__version__,
        description="HITL multi-agent research automation. See SPEC.md for the contract.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_prefix = "/api/v1"
    app.include_router(health.router, prefix=api_prefix)
    app.include_router(projects.router, prefix=api_prefix)
    app.include_router(workflow.router, prefix=api_prefix)
    app.include_router(papers.router, prefix=api_prefix)
    app.include_router(artifacts.router, prefix=api_prefix)
    app.include_router(websocket.router, prefix=api_prefix)

    return app


app = create_app()
