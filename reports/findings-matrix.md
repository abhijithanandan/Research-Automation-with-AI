# Findings Matrix — `audit/2026-05-31`

Unified inventory of issues discovered by:
1. Prior manual audit pass (carry-forward, captured in `baseline.md`).
2. Step 2 automated scanners — `bandit`, `radon`, `eslint`, `tsc`, `npm audit`.
3. Step 3 heuristic code-review pass (workflow replay, auth boundaries, data integrity, I/O safety).

Findings are grouped by **domain**, not by frontend/backend split, so each row tells you who owns the fix.

## Severity model

| Severity | Definition | Merge policy |
| --- | --- | --- |
| **Critical** | Active exploit, data loss, auth bypass, RCE | Block merge. Hotfix required. |
| **High** | Exploit path exists, contract-breaking bug, integrity hole, stale-dep CVE that's directly exploitable | Block merge to `main`. Land in Wave 1. |
| **Medium** | Reliability or correctness gap that degrades UX or audit-trail value; doesn't expose data | Land in Wave 2. |
| **Low** | Maintainability, polish, type-safety improvement, defensive correctness | Wave 3, ongoing. |

## Inventory by domain

### Domain 1 — Workflow Engine (LangGraph + service layer)

| ID | Severity | File:Line | Issue | Impact | Exploitability | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| W1 | Low | `backend/app/services/workflow.py:881,972` | Bandit B101 — `assert` used as a runtime invariant (refreshed run row, contract state) | Stripped under `PYTHONOPTIMIZE=1`; current Docker image does not set it. Defensive only. | None | Backend |
| W2 | Low | `frontend/app/page.tsx:228` | Duplicate `approval.required{phase:drafting}` events trigger two artifact fetches (replay + live) | 2 redundant GETs + 2 telemetry refetches; UI converges correctly | None | Frontend |

### Domain 2 — Agent Layer (Librarian / Critic / Scribe)

| ID | Severity | File:Line | Issue | Impact | Exploitability | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| A1 (was H1) | **High** | `backend/app/agents/scribe.py:398-401`, `backend/app/agents/critic.py:37-47` | Indirect Prompt Injection — paper title/abstract interpolated raw into LLM prompts (no XML wrap) | Poisoned upstream metadata can override system prompt → forged sections, false citations | Network-adjacent: requires malicious mirror or compromised arXiv/CORE row. Trivial to weaponize once one row lands. | Backend |
| A2 (was M3) | Medium | `backend/app/agents/librarian.py:209-254` | `_deduplicate` is O(n²) in the no-DOI path | At current `MAX_PAPER_CANDIDATES=30` it's ~56ms. Quadratic past ~150 candidates. | None | Backend |
| A3 (new — bandit B112) | Low | `backend/app/services/fulltext_fetcher.py:257` | `try/except/continue` swallows the exception class | A real parse failure logs nothing; the per-paper graceful-degrade is intentional but the silent-swallow is loud-logging gap | None | Backend |

### Domain 3 — Security & Auth

| ID | Severity | File:Line | Issue | Impact | Exploitability | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| S1 (new — bandit B314/B405) | **High** | `backend/app/services/discovery.py:15,252` | XXE / billion-laughs on arXiv XML — uses stdlib `xml.etree.ElementTree.fromstring` | Memory exhaustion or file exfiltration from a malicious mirror / MITM payload | Requires upstream compromise; arXiv is trusted today. Defense-in-depth fix. | Backend |
| S2 (was H3 — confirmed by `npm audit`) | **High** | `frontend/package.json` `"next":"14.2.5"` | Multiple GHSAs: cache poisoning, image-opt DoS, server-actions DoS, dev-server info-exposure, image-opt key confusion | Cache poisoning is the worst — attacker who can poison the route can serve arbitrary content to other users. | Internet-facing; depends on deployment topology. | Frontend |
| S3 (was H2) | **High** | `backend/app/services/citations.py:96-105`, `backend/app/api/routes/workflow.py:179-194` | `citation_corrections` accept arbitrary keys (no pool-membership validation) | Defeats FR-1.5 cite-only-from-pool contract; audit log lies about "corrected to valid key" | Authenticated user only; full integrity hole on academic submissions | Backend |
| S4 (was M1) | Medium | `backend/app/api/routes/workflow.py:91,142,160` | Missing per-user rate limit on `/approve` `/reject` `/override` | Authenticated user can spam override (256 KB each) → DB+audit_log fill | Authenticated; high blast-radius if any DEV_AUTH_BYPASS or compromised token | Backend |
| S5 (was M2) | Medium | `backend/app/api/routes/workflow.py:42-51` | Server-side `override_reason` not enforced when `force_unresolved=true` | Audit log records forced approvals with empty reason → integrity hole | Authenticated client bypasses frontend disable | Backend |
| S6 (new — heuristic) | Medium | `backend/app/services/discovery.py:140 etc.` | tenacity retries on 429 ignore `Retry-After` header | A 429 burst exhausts 3 attempts in ~7s instead of waiting; source goes silent for the run | None — DoS-resilience only | Backend |
| S7 (carried from bandit summary) | Low | `backend/app/agents/librarian.py:111`, `backend/app/main.py:68` | More `assert` runtime invariants (same class as W1) | Stripped under `-O` | None | Backend |

### Domain 4 — Data & Persistence

No new findings. Existing posture:
- 4 partial unique indexes (workflow_runs.active, papers.citation_key, artifacts.manuscript, audit.pool_approval) — verified via `app/models/db.py`.
- 1 CHECK constraint (`workflow_runs.state IN (...)`) — alembic 0007.
- All inserts use `ON CONFLICT DO NOTHING` where partial uniques apply.
- All SQL is parameterized; no f-string interpolation found via Grep.

### Domain 5 — Client Reliability

| ID | Severity | File:Line | Issue | Impact | Exploitability | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| C1 (was M4) | Medium | `backend/app/services/fulltext_fetcher.py:79-137` | Sequential PDF fetch blocks workflow ~120s with no progress signal | "Critic is starting…" message + dead silence; users reload, breaking the run | None | Backend + Frontend |
| C2 (was L1) | Low | `backend/app/models/schemas.py:65` vs `frontend/lib/types.ts:80` | `Paper.project_id` nullable on backend vs required on TS | Would crash UI if backend ever returned `null` (it doesn't) | None | Both |
| C3 (was L2) | Low | `frontend/lib/types.ts:78-91` | TS `Paper` missing `citation_count` | Field silently dropped; UI never displays it | None | Frontend |
| C4 (was L4) | Low | `frontend/lib/api.ts:203,217,224,242` | `api.workflow.*` returns typed `unknown` | TS doesn't catch wire shape drift | None | Frontend |
| C5 (was L5) | Low | `frontend/app/page.tsx:371,406,419` | Handlers not `useCallback`-wrapped | Future-perf footgun; no current cost | None | Frontend |

### Domain 6 — DX / CI

| ID | Severity | File:Line | Issue | Impact | Exploitability | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| D1 (was L3) | Low | `backend/.env.example` | Missing `UNPAYWALL_EMAIL`, `CROSSREF_MAILTO`, `CORE_API_KEY` | Fresh-clone discovery yield is lower than possible | None | Backend |
| D2 (Step 6) | Medium | `backend/run_ci_local.sh` (target) | No CI gate enforces bandit + radon + npm-audit | Regressions slip in; baseline drifts | None | DX |
| D3 (Step 6) | Medium | GitHub branch protection | No required-checks list on `main` | Anyone with push rights can merge through red | None | DX |
| D4 (hybrid-search) | Low | `app/services/discovery.py:201` `SemanticScholarAdapter._search_with_retry` | Rank-D (CC 21) un-waived; the 4 identical sibling adapters were already waived, this one was missed | None (API surface-area complexity, not logic) | Backend — radon waiver added |
| D5 (hybrid-search) | Low | `app/services/workflow.py:1495` `_handle_analysis_gate_pause` | Rank-D (CC 21+) two-branch Phase-3 HITL gate re-arm; defensive isinstance guards over graph-state dicts | None | Backend — radon waiver added; refactor candidate if a 3rd sub-gate is ever added |

## Counts

| Severity | Count |
| --- | --- |
| Critical | 0 |
| High | **4** (A1, S1, S2, S3) |
| Medium | **7** (A2, S4, S5, S6, C1, D2, D3) |
| Low | **8** (W1, W2, A3, S7, C2, C3, C4, C5, D1) — 9 if you split S7 into its 4 individual `assert` sites |

## Mapping note — carry-forward IDs

| Prior audit ID | New matrix ID |
| --- | --- |
| H1 → A1 | Agent layer |
| H2 → S3 | Security & Auth |
| H3 → S2 | Security & Auth |
| H4 (new from bandit) | S1 |
| M1 → S4 | Security & Auth |
| M2 → S5 | Security & Auth |
| M3 → A2 | Agent layer |
| M4 → C1 | Client Reliability |
| L1 → C2 | Client Reliability |
| L2 → C3 | Client Reliability |
| L3 → D1 | DX/CI |
| L4 → C4 | Client Reliability |
| L5 → C5 | Client Reliability |
| L6 (new from heuristic — duplicate WS refetch) | W2 |
| M5 (new from heuristic — Retry-After ignored) | S6 |
| Bandit B101 (4×) | W1 + S7 |
| Bandit B112 | A3 |
