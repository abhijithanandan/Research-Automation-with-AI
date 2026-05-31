# Audit Baseline — 2026-05-31

Frozen environment snapshot for the `audit/2026-05-31` branch. Every scanner run downstream targets the versions below; rerun the scanners after any dependency bump and diff against the original artifacts in `reports/`.

## Branch

| Field | Value |
| --- | --- |
| Branch | `audit/2026-05-31` |
| Cut from | `feature/phase-4` @ `95d227e` |
| Created | 2026-05-31 |
| Working tree | clean |

## Runtimes

| Runtime | Version |
| --- | --- |
| Backend host venv Python | 3.13.9 |
| Backend container Python | 3.11 (Dockerfile baseline) |
| Frontend container Node | v20.20.2 |
| Frontend container npm | 10.8.2 |
| Frontend container npx | 10.8.2 |

## Preflight

`backend\scripts\preflight.py` ran clean. 28 required modules importable; the compatibility-pinned trio (`pydantic` / `langchain-core` / `langgraph`) is version-aligned per the `_COMPAT_TRIO` constant.

## Backend dependency pins (selected, in-venv)

```
alembic                           1.18.4
anthropic                         0.104.1
asyncpg                           0.31.0
bandit                            1.9.4
chromadb                          1.5.9
fastapi                           0.136.1
google-genai                      2.6.0
httpx                             0.28.1
langchain                         1.3.1
langchain-core                    1.4.0
langgraph                         1.2.1
langgraph-checkpoint              4.1.0
langgraph-checkpoint-postgres     3.1.0
mypy                              2.1.0
psycopg                           3.3.4
pydantic                          2.13.4
pydantic-settings                 2.14.1
pypdf                             6.12.1
radon                             6.0.1
```

Full lock: see [`backend/requirements-lock.txt`](backend/requirements-lock.txt).

## Frontend dependency pins (selected, from package.json)

```
next                14.2.5
react               18.3.1
react-dom           18.3.1
react-markdown      ^10.1.0
tailwindcss         ^4.3.0
typescript          ^5.5.0
vitest              ^2.0.0
clsx                ^2.1.1
tailwind-merge      ^2.4.0
```

Full lock: see [`frontend/package-lock.json`](frontend/package-lock.json).

## Audit-tool versions

| Tool | Version |
| --- | --- |
| bandit | 1.9.4 |
| radon | 6.0.1 |
| ruff | 0.15.15 |
| mypy | 2.1.0 |
| pytest | 9.0.3 |
| pip-audit | 2.10.0 (declared in pyproject) |

## Baseline test posture

- `pytest -q` → 294 passed, 1 pre-existing warning. Captured pre-audit.
- `ruff check app/ tests/` → clean.
- `ruff format --check app/ tests/` → 76 files formatted, clean.
- `mypy --strict app/` → 41 source files, 0 issues.
- Frontend `npx tsc --noEmit` → clean.
- Frontend `npx next lint` → 1 pre-existing warning (`no-page-custom-font`).

## Carry-forward findings (from the prior manual audit — see prior session)

These ride forward into `reports/findings-matrix.md` and are not re-discovered by the automated tools (they are architectural / wire-level / dep-version findings the static scanners cannot see directly):

- **H1** Indirect Prompt Injection — Scribe + Critic interpolate untrusted abstracts without XML encapsulation.
- **H2** `citation_corrections` accept arbitrary replacement keys (audit-integrity bypass).
- **H3** Next.js 14.2.5 has stale CVEs (cache poisoning, middleware bypass).
- **M1** Missing rate limits on `/workflow/approve|reject|override`.
- **M2** Server-side `override_reason` not enforced when `force_unresolved=true`.
- **M3** `_deduplicate` is O(n²) in the no-DOI path.
- **M4** Fulltext fetch blocks workflow ~120s with no progress signal.
- **L1** Pydantic `Paper.project_id: UUID | None` vs TS `string` required.
- **L2** Pydantic `Paper.citation_count` missing in TS `Paper`.
- **L3** `.env.example` missing 3 optional discovery keys.
- **L4** `api.workflow.*` returns typed as `unknown`.
- **L5** Approve/Reject/Override handlers not `useCallback`-wrapped.
