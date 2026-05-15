"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import health, projects, workflow, papers, artifacts, websocket
from app.config import get_settings
from app.utils.logging import configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    # TODO: open db pool, init vector store client, warm LLM gateway.
    yield
    # TODO: close db pool, flush logs.


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
