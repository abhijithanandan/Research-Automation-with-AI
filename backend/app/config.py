"""Application configuration. All env-driven settings live here."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    cors_allowed_origins: str = "http://localhost:3000"

    llm_provider: Literal["gemini", "openai", "anthropic", "deepseek"] = "gemini"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.5-pro"

    database_url: str = "postgresql+asyncpg://researchflow:researchflow@localhost:5432/researchflow"

    vector_db_url: str = "http://localhost:8001"
    vector_db_provider: Literal["chroma", "pinecone", "qdrant"] = "chroma"

    firebase_project_id: str = ""
    firebase_credentials_path: str = ""
    firebase_credentials_json: str = ""

    default_token_cap_usd: float = 5.0
    max_paper_candidates: int = Field(default=30, ge=1, le=200)

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
