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

    # Hard production-safety guard: DEV_AUTH_BYPASS makes every request
    # authenticate as the fixed dev user. Shipping that in staging or
    # production would let any client impersonate any user. Refuse to boot
    # rather than rely on operators noticing a misconfiguration (audit
    # round-3, EXPLOIT-1).
    if settings.dev_auth_bypass and settings.app_env != "development":
        _log.error(
            "dev_auth_bypass_in_non_dev",
            app_env=settings.app_env,
        )
        raise RuntimeError(
            "DEV_AUTH_BYPASS=true is only permitted with APP_ENV=development. "
            f"Refusing to start with APP_ENV='{settings.app_env}'."
        )

    # Boot-time security posture row — every start is recorded so a future
    # audit can prove what the running app's auth config actually was
    # (M1-B). Token cap + cors origins are surfaced because a misconfigured
    # CORS origin is a common XSS-adjacent footgun.
    _log.info(
        "app.start",
        app_env=settings.app_env,
        dev_auth_bypass=settings.dev_auth_bypass,
        cors_origins=settings.cors_origins_list,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
    )

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
            # Structured field per M1-D — log the exception class so log
            # consumers can group failures without parsing free-form text.
            _log.warning(
                "failed_to_cleanup_orphaned_runs",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    # Run cleanup before graph initialization
    await _cleanup_orphaned_runs()

    checkpointer = None

    try:
        checkpointer = await create_postgres_checkpointer(settings.database_url)
        _log.info("checkpointer_postgres_ready")
    except Exception as exc:
        # Postgres unavailable. In development we degrade to an in-memory
        # checkpointer so the app still boots without Docker. Outside of
        # development this is a hard failure — silently losing workflow
        # state across restart would shred the audit trail (audit round-3,
        # HIGH-2). Refuse to start and let the operator fix Postgres.
        if settings.app_env != "development":
            _log.error(
                "checkpointer_postgres_required",
                app_env=settings.app_env,
                reason=str(exc),
            )
            raise RuntimeError(
                "Postgres checkpointer is required outside development. "
                f"App refusing to start with app_env='{settings.app_env}'. "
                "Fix DATABASE_URL or set APP_ENV=development for local work."
            ) from exc

        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
        _log.warning(
            "checkpointer_fallback_memory",
            reason=str(exc),
            hint="Start Docker (docker compose up postgres) for persistent state.",
        )

    await init_graph(checkpointer)

    yield

    if hasattr(checkpointer, "conn") and hasattr(checkpointer.conn, "close"):
        await checkpointer.conn.close()
    elif hasattr(checkpointer, "aclose"):
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

    # Body-size cap (M1-C). Mounted *before* the routers so unauthenticated
    # peers can't soak memory by sending a 5 GiB JSON blob.
    from app.api.middleware import BodySizeLimitMiddleware

    app.add_middleware(BodySizeLimitMiddleware)

    api_prefix = "/api/v1"
    app.include_router(health.router, prefix=api_prefix)
    app.include_router(projects.router, prefix=api_prefix)
    app.include_router(workflow.router, prefix=api_prefix)
    app.include_router(papers.router, prefix=api_prefix)
    app.include_router(artifacts.router, prefix=api_prefix)
    app.include_router(websocket.router, prefix=api_prefix)

    return app


app = create_app()
