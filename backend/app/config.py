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
    llm_model: str = "gemini-3.5-flash"

    database_url: str = "postgresql+asyncpg://researchflow:researchflow@localhost:5432/researchflow"

    vector_db_url: str = "http://localhost:8001"
    vector_db_provider: Literal["chroma", "pinecone", "qdrant"] = "chroma"

    # --- Hybrid search (Critic RAG, Phase 2) -------------------------------
    # Master switch for the BM25 + dense + RRF retrieval path. Default OFF so
    # the existing dense-only behaviour is unchanged until an operator opts in
    # (Zero-Regression policy). When False, `hybrid_reranked_search` is exactly
    # the legacy dense `query` and no BM25 corpus is built on upsert.
    hybrid_search_enabled: bool = False
    # How many candidates each retriever (dense, sparse) contributes to the
    # fusion pool before reranking.
    hybrid_dense_top_k: int = Field(default=30, ge=1, le=200)
    # RRF constant — higher dampens the contribution of lower-ranked hits.
    hybrid_rrf_k: int = Field(default=60, ge=1)
    # Final number of chunks returned to the Critic after reranking.
    hybrid_top_n: int = Field(default=6, ge=1, le=50)
    # Cross-encoder reranking (stage 3). Requires the optional [rerank] extra
    # (sentence-transformers). When True but the library/model is unavailable
    # the path logs once and degrades to RRF-only — it never hard-fails.
    rerank_enabled: bool = False
    rerank_model: str = "BAAI/bge-reranker-base"

    firebase_project_id: str = ""
    firebase_credentials_path: str = ""
    firebase_credentials_json: str = ""

    # Set to True in local dev to skip Firebase token verification.
    # Never enable in staging or production.
    dev_auth_bypass: bool = False

    # Optional Semantic Scholar API key (raises the rate limit 100→1000 req/min,
    # avoiding the 429 throttling that otherwise drops SS from discovery results).
    semantic_scholar_api_key: str = ""

    # Optional contact email — puts Crossref requests in the faster "polite pool".
    crossref_mailto: str = ""

    # Optional CORE API key — required for api.core.ac.uk/v3 access. Free tier
    # registration at https://core.ac.uk/services/api. Without a key the CORE
    # adapter degrades to a no-op (logs once and returns []), so the rest of
    # the discovery pipeline keeps working.
    core_api_key: str = ""

    # Contact email for the Unpaywall API. Their ToS asks every request to
    # identify the caller — without an email the unpaywall enricher is a
    # no-op so we never send anonymous traffic to their service.
    unpaywall_email: str = ""

    # Local-filesystem root for dataset storage (Phase 3 / FR-2.3). In prod
    # the storage adapter switches to object storage. Files land under
    # DATA_DIR/<project_id>/<dataset_id>/.
    data_dir: str = "./data"
    # Hard cap on a single dataset upload, in bytes. The HTTP body-size
    # middleware drops larger requests before they hit the handler; this is
    # a belt-and-braces guard inside the parser.
    max_dataset_bytes: int = 50 * 1024 * 1024  # 50 MiB

    # Phase 3 sandbox — Docker per-call (T2). Image must contain numpy,
    # pandas, matplotlib, scipy, scikit-learn at minimum.
    sandbox_image: str = "researchflow-analyst:0.2"
    sandbox_timeout_s: int = 60
    sandbox_memory_mb: int = 512
    sandbox_cpus: float = 1.0
    # When false the sandbox service refuses to run (defense-in-depth for
    # staging hosts that haven't been hardened). Must be explicitly enabled
    # in env to start executing user-generated code.
    sandbox_enabled: bool = False

    default_token_cap_usd: float = 5.0
    max_paper_candidates: int = Field(default=30, ge=1, le=200)
    # Warn when this fraction of the per-project token cap is consumed (BRD §NFR-5).
    token_cap_warn_pct: float = Field(default=0.8, ge=0.0, le=1.0)

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
