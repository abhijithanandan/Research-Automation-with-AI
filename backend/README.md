# ResearchFlow AI — Backend

FastAPI + LangGraph remote engine. Read the root `SPEC.md` before editing this code.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in keys
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for the auto-generated OpenAPI explorer.

## Layout

```
app/
├── main.py          # FastAPI factory
├── config.py        # Pydantic settings
├── api/
│   ├── deps.py      # auth + DI
│   └── routes/      # HTTP + WS routes
├── agents/          # Librarian / Critic / Analyst / Scribe
├── graph/           # LangGraph state machine
├── models/
│   ├── schemas.py   # Pydantic wire types (match SPEC.md §2.2)
│   └── db.py        # SQLAlchemy ORM
├── services/        # llm, vector_store, auth adapters
└── utils/
tests/               # pytest
```

## Testing

```bash
pytest -q
ruff check .
ruff format --check .
mypy app
```

## Architectural rules (non-negotiable)

1. Approval gates live in `app/graph/` and are enforced *there*, not in `app/api/`.
2. Every agent invocation writes to `audit_log` before its result is returned.
3. LLM calls go through `app.services.llm.LLMGateway` — never call providers directly.
4. New endpoints require updates to `docs/api/openapi.yaml` *in the same PR*.
