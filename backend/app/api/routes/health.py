"""Health + meta routes."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import get_settings

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/meta/providers")
async def providers() -> dict[str, object]:
    settings = get_settings()
    return {
        "default": settings.llm_provider,
        "available": ["gemini", "openai", "anthropic", "deepseek"],
    }
