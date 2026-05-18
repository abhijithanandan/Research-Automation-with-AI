# ResearchFlow AI — Agentic Research Automation System

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Next.js](https://img.shields.io/badge/Next.js-14-black)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)
![LangGraph](https://img.shields.io/badge/LangGraph-Agentic-orange)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Status](https://img.shields.io/badge/status-Phase%201%20Hardened-green.svg)

ResearchFlow AI is a hybrid, multi-agent AI workflow designed to accelerate academic and technical research. The system automates repetitive tasks across the research lifecycle — from literature discovery through manuscript drafting — while keeping a human firmly in control of every consequential decision.

A defining principle is **Strict Human-in-the-Loop (HITL) Orchestration**: the AI is a co-pilot, not a replacement. The state machine *cannot* advance between phases without an explicit human approval event.

> [!NOTE]
> **Status:** **Phase 1 (Query & Discovery)** is fully implemented, verified, and hardened. The Librarian agent supports semantic velocity ranking, strict abstract XML encapsulation to prevent indirect prompt injections, robust BibTeX key collision resolution (>26 items), and startup reconciliation for orphaned runs. The backend passes strict `mypy --strict` and `ruff` formatting, with a 100% pass rate in our `pytest` suites.

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Repository layout](#repository-layout)
- [System architecture](#system-architecture)
- [Agentic personas](#agentic-personas)
- [Getting started](#getting-started)
- [The HITL workflow](#the-hitl-workflow)
- [Environment variables](#environment-variables)
- [Development workflow](#development-workflow)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Documentation map](#documentation-map)
- [License](#license)

---

## Why this exists

Research students spend 40–60% of their early project time on mechanical tasks: searching databases, deduplicating papers, building comparison matrices, formatting citations, and reshaping prose. Existing tools solve isolated slices; fully-autonomous "AI researcher" tools violate academic integrity norms. ResearchFlow AI sits in the middle — orchestrating an end-to-end research workflow while keeping the human as the author of record at every phase boundary.

See [`BRD.md`](./BRD.md) for the full business and functional requirements.

---

## Repository layout

```
.
├── BRD.md                  # Business + functional requirements
├── SPEC.md                 # Technical spec (data models, APIs, events)
├── ARCHITECTURE.md         # Architecture deep-dive + diagrams
├── CONTRIBUTING.md         # Contribution + branching + review process
├── docker-compose.yml      # Local dev: Postgres + Chroma + backend + frontend
├── docs/
│   ├── api/openapi.yaml    # REST contract (source of truth)
│   ├── agents/             # Per-agent contracts (Librarian, Critic, ...)
│   └── workflow/           # State-machine specs
├── backend/                # FastAPI + LangGraph engine
│   ├── app/
│   │   ├── api/            # HTTP + WebSocket routes
│   │   ├── agents/         # The four persona implementations
│   │   ├── graph/          # LangGraph state machine
│   │   ├── models/         # Pydantic models + ORM models
│   │   └── services/       # LLM, vector store, auth adapters
│   ├── tests/
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/               # Next.js 14 (App Router) client
│   ├── app/                # Routes
│   ├── components/         # UI building blocks
│   ├── lib/                # API client, WS client, shared types
│   ├── package.json
│   └── Dockerfile
└── .github/workflows/      # CI for backend + frontend
```

A walk-through of why the project is split this way lives in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## System architecture

ResearchFlow AI uses a **Hybrid Client-Server Architecture** to balance computational intensity, data privacy, and academic-site access.

- **Local Client (Next.js):** UI, workflow visualization, local PDF parsing, and a Playwright-driven browser-automation agent that runs under the user's residential IP — avoiding cloud-IP bot detection on publisher sites.
- **Remote Engine (FastAPI):** LangGraph orchestration, LLM inference, vector embeddings, and (post-MVP) sandboxed code execution.
- **Communication:** REST for command/control + WebSocket for streaming agent tokens and live state events.
- **Data layer:** PostgreSQL for project metadata and workflow state; an object store for artifacts; a vector store (Chroma in dev, Pinecone/Qdrant in prod) for RAG.

```
┌────────────────────────┐     REST + WS      ┌─────────────────────────┐
│  Next.js Local Client  │ ◄────────────────► │   FastAPI Remote Engine │
│  • Dashboard           │                    │  • LangGraph state       │
│  • Approval panels     │                    │  • LLM gateway           │
│  • PDF parser          │                    │  • RAG retrieval         │
│  • Playwright (local)  │                    │  • Sandbox (v0.2)        │
└─────────┬──────────────┘                    └───┬─────────────┬───────┘
          │                                       │             │
          ▼                                       ▼             ▼
   Local FS + user IP                       Postgres        Vector DB
                                            (state)         (RAG)
```

---

## Agentic personas

| Persona | Role | Primary tools |
| --- | --- | --- |
| **The Librarian** | Discovery: queries APIs, expands keywords, deduplicates, ranks. | Semantic Scholar, ArXiv, Crossref, (post-MVP) Playwright. |
| **The Critic** | Synthesis: extracts methodology, builds comparison matrix, narrative summary. | RAG over approved paper pool. |
| **The Analyst** | Compute (v0.2): writes Python, executes in a sandbox, returns plots/tables/logs. | Python sandbox, scientific libs. |
| **The Scribe** | Drafting: section-by-section academic prose with strict citation discipline. | RAG, BibTeX renderer, Markdown/LaTeX writer. |

Each agent's input/output contract is documented under [`docs/agents/`](./docs/agents/).

---

## Getting started

### Prerequisites

- **Node.js** ≥ 18.18 (Next.js 14 requirement)
- **Python** ≥ 3.11
- **Docker** + Docker Compose (for Postgres + Chroma in dev)
- **An LLM API key** — Gemini, OpenAI, Anthropic, or DeepSeek (see `backend/.env.example` for the supported variable names)

### Quick start (recommended — Docker Compose)

```bash
git clone git@github.com:abhijithanandan/Research-Automation-with-AI.git
cd Research-Automation-with-AI

# Install repo-root dev tooling (Husky hooks, lint-staged, commitlint, Prettier).
# This is required for the pre-commit hooks to activate on your machine.
npm install

cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
# Open both files and fill in at least one LLM API key.

docker compose up --build
```

- Backend: http://localhost:8000 (docs at `/docs`)
- Frontend: http://localhost:3000

### Manual setup

If you prefer running services on the host:

**Backend**
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in keys
uvicorn app.main:app --reload --port 8000
```

**Frontend**
```bash
cd frontend
npm install
cp .env.example .env.local  # point NEXT_PUBLIC_API_BASE_URL at backend
npm run dev
```

---

## The HITL workflow

The engine enforces approval gates at the state-machine level — *not* the UI level. A misbehaving client cannot bypass them.

1. **Discovery** — Librarian fetches papers → **pause** → user approves the paper pool.
2. **Synthesis** — Critic builds the literature matrix and summary → **pause** → user edits / approves.
3. **Analysis** *(v0.2)* — Analyst writes and executes code → **pause** → user reviews artifacts and code log.
4. **Drafting** — Scribe writes one section → **pause** → user edits / approves → next section.

At every paused state the user can: **Approve**, **Reject & Regenerate** (with feedback), or **Manual Override** (direct edit). See `docs/workflow/state-machine.md` for the formal state diagram and event contract.

---

## Environment variables

All variables are documented inline in `.env.example` files. Headlines:

**Backend (`backend/.env`)**
- `LLM_PROVIDER` — one of `gemini` | `openai` | `anthropic` | `deepseek`
- `LLM_API_KEY` — the corresponding key
- `DATABASE_URL` — Postgres connection string
- `VECTOR_DB_URL` — Chroma endpoint (or Pinecone/Qdrant creds)
- `FIREBASE_PROJECT_ID` / `FIREBASE_CREDENTIALS_JSON` — for auth verification
- `CORS_ALLOWED_ORIGINS` — comma-separated; include the frontend dev origin

**Frontend (`frontend/.env.local`)**
- `NEXT_PUBLIC_API_BASE_URL` — e.g. `http://localhost:8000`
- `NEXT_PUBLIC_WS_BASE_URL` — e.g. `ws://localhost:8000`
- `NEXT_PUBLIC_FIREBASE_*` — Firebase web SDK config

---

## Development workflow

1. Read `BRD.md` → `SPEC.md` → the agent contract under `docs/agents/`.
2. Create a feature branch off `main`: `feature/<scope>-<short-name>`.
3. Update specs *first* when changing a contract. The OpenAPI file and the agent contract docs are the source of truth — code follows them, not the other way around.
4. Commit with conventional messages (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `spec:`). The `commit-msg` hook enforces this.
5. Pre-commit hooks (Prettier, ESLint --fix, Ruff --fix, Ruff format) run automatically on staged files. If a hook rewrites a file, re-stage and commit again.
6. Open a PR against `main`. Fill in the template (linked specs, screenshots, test plan).
7. At least one approving review required. CI must be green.

The full process — including the hook setup, the commitlint contract, and how to recover from rejected commits — is in [`CONTRIBUTING.md`](./CONTRIBUTING.md).

---

## Testing

- **Backend:** `cd backend && pytest`. Unit tests for agents are mock-based; HITL graph behavior is tested with an in-memory checkpointer.
- **Frontend:** `cd frontend && npm run test` (Vitest) and `npm run e2e` (Playwright, post-MVP).
- **Lint/Type:** `ruff check` + `mypy` on backend; `eslint` + `tsc --noEmit` on frontend. All four run in CI.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Connection refused` on the frontend | Backend not running, or `NEXT_PUBLIC_API_BASE_URL` is wrong. | Confirm backend is up on the expected port; restart `npm run dev` after editing `.env.local`. |
| 401 from `/projects` endpoints | Firebase token missing or expired. | Re-login in the frontend; verify `FIREBASE_PROJECT_ID` matches between client and backend. |
| Playwright fails to install browsers | Sandbox lacks system libs. | Run `npx playwright install --with-deps` inside the frontend container. |
| LLM call returns 429 | Rate-limited by the provider. | Lower the project's concurrency, or switch `LLM_PROVIDER`. |
| Vector store returns empty hits | Embeddings not yet flushed for the project. | Check the Critic logs; embedding happens lazily after Phase 1 approval. |

---

## Documentation map

| Doc | What it covers |
| --- | --- |
| [`BRD.md`](./BRD.md) | Business + functional requirements; what we're building and why. |
| [`SPEC.md`](./SPEC.md) | Technical spec: data models, REST + WS contracts, state machine. |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Architecture deep-dive, deployment topology, data flows. |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | How to branch, commit, review, and ship. |
| `docs/api/openapi.yaml` | REST contract (source of truth). |
| `docs/agents/*.md` | Per-agent I/O contracts. |
| `docs/workflow/state-machine.md` | Formal state machine diagram + event contract. |

---

## License

MIT — see [`LICENSE`](./LICENSE).
