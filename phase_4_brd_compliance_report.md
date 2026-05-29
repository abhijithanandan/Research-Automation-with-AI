# Phase 4 BRD/FRD Compliance Report — ResearchFlow AI

**Audit branch:** `feature/phase-4` (HEAD `e1adf30` — _feat(ui): pitch-black/emerald overhaul + Level-3 terminal polish_)
**Source-of-truth docs:** `BRD.md` v1.1 · `SPEC.md` v0.1 · `ARCHITECTURE.md` · `docs/agents/*.md`
**Audited by:** Claude Code (Opus 4.7) — read-only inspection + test-suite run
**Audit date:** 2026-05-29
**Test state at audit:** `pytest` **209 passed, 1 warning** (full backend suite, real run)

---

## 0. TL;DR

| Verdict | The MVP (BRD §8 v0.1 scope = Phases 1, 2, 4) is **functionally complete and compliant**. Every HITL gate is enforced at the state-machine level, the citation invariant holds, and the cost cap is wired end-to-end. The remaining gaps are **spec-contract drift** (export endpoint, error envelope, trace_id) and **partial UI polish** (no persistent spend meter, no inline BibTeX editor) — none are MVP blockers. |
| --- | --- |

**What's solid (verified):**
- ✅ All four phase gates wired in LangGraph with `interrupt()` + approve/reject/override routing (BRD §4.1, SPEC §5.2). The Phase-1 report's §5.6 "synthesize→draft with no gate" bug is **fixed**.
- ✅ The four Phase-1 blockers (B1–B4) are all closed: candidates persist, approved-pool snapshot built, race handled, override writes a `produced_by="human"` artifact.
- ✅ Scribe citation invariant enforced post-generation with one retry + `INVALID:` surfacing (FR-2.4, risk #1).
- ✅ Cost cap (NFR-5) computes real `cost_usd` from per-model pricing + real Gemini/Anthropic token counts, rolls up per-project, warns at `warn_pct`, halts at cap.
- ✅ Users-upsert on auth (Phase-1 §5.5 finding fixed) — `owner_id` FK now resolves on real Postgres.
- ✅ LLM provider abstraction with **two** providers live (Gemini + Anthropic) — exceeds MVP's "one provider"; risk #6 (vendor lock-in) mitigated.

**Gaps to address (none MVP-blocking):**
- ⚠️ `GET /export` (SPEC §3.5) is a **501 stub** even though the manuscript is assembled and downloadable client-side.
- ⚠️ Error envelope is FastAPI's `{detail:{…}}`, not the spec'd `{error:{code,message,trace_id}}` (SPEC §3.7).
- ⚠️ `trace_id` (NFR-6) still not wired — no UI→API→LLM trace linking. Carried over from Phase-1 §5.2.
- ⚠️ No persistent token-spend meter in the dashboard (FR-1.1) — cap warnings are transient log lines only.
- ⚠️ No inline BibTeX preview / manual citation-key correction UI (FR-1.5).
- ⚠️ NFR-7 (WCAG 2.1 AA) — good a11y signals but no formal audit; PhaseTracker/Markdown lack aria.

---

## 1. MVP scope alignment (BRD §8)

| BRD §8 item | Status | Evidence |
| --- | --- | --- |
| Single-user, single-project | ✅ | One project flow; no multi-project routing (correctly absent). |
| Phases 1, 2, 4 (skip Analyst/Phase 3) | ✅ | `analyst.py` raises `NotImplementedError` ("scheduled for v0.2"); graph wires discover→synthesize→draft→assemble, no `analyze` node. |
| Semantic Scholar + ArXiv | ✅ (exceeded) | Five adapters present: SS, arXiv, Crossref, CORE, Europe PMC (`services/discovery.py`). |
| Markdown output (LaTeX in v0.2) | ✅ | Scribe raises explicit error on `output_format=="latex"`; Markdown only. |
| Chroma + Postgres + one LLM | ✅ (exceeded) | Chroma vector store + Postgres checkpointer; **two** LLM providers wired. |
| **Out of scope:** Phase 3, browser scraping, LaTeX, multi-LLM fallback, multi-project | ✅ correctly absent / deferred | Analyst stub; no Playwright anywhere; LaTeX gated; single active provider at a time. |

**MVP scope verdict: fully aligned.** The build matches v0.1 scope and over-delivers on discovery sources and provider count.

---

## 2. Functional Requirements coverage

### 2.1 Local Client (FR-1.x)

| FR | Requirement | Status | Notes |
| --- | --- | --- | --- |
| FR-1.1 | Dashboard: phase state, current phase, pending approvals, agent activity, **token spend** | ⚠️ Partial | Phase tracker (vertical stepper), live agent log, approval panels all present. **Token-spend display is missing** — `cost.cap_warn/exceeded` show as transient log lines; no running spend meter and no `/usage` fetch on the dashboard. |
| FR-1.2 | Local PDF upload + parse → chunk → embed | ⚠️ Partial / deferred | `POST /papers/upload` = **501** (BRD §8 marks PDF upload out of MVP). The `fulltext_fetcher` service covers the *spirit* (auto-downloads OA PDFs the source APIs expose → pypdf → Chroma), but **user-initiated local upload is not implemented**. |
| FR-1.3 | Local Playwright browser automation | ⛔ Not implemented (correctly) | BRD §8 + §12 defer browser-use to v0.3. No Playwright in the tree. |
| FR-1.4 | Approval UI: paper selector (P1), diff/edit (P2/P4), plot/code (P3) | ✅ (P1/P2/P4) | `ApprovalPanel`, `SynthesisReview` (matrix + narrative tabs, diff view, override editor), `SectionReview` (per-section, diff, citations tab). P3 viewer correctly absent. |
| FR-1.5 | Citation manager: inline BibTeX preview + manual correction before approve | ⚠️ Partial | Citation keys shown on papers/matrix; `INVALID:` citation chips surfaced in SectionReview. **No inline BibTeX preview and no dedicated citation-key editor** — correction is only possible via free-form override of section content. |

### 2.2 Core Agentic Personas (FR-2.x)

| FR | Requirement | Status | Notes |
| --- | --- | --- | --- |
| FR-2.1 | Librarian: SS+ArXiv+Crossref, query expansion, dedup (DOI + fuzzy title), ranked candidates | ✅ (exceeded) | LLM query expansion; 5 source adapters; dedup by DOI then `token_set_ratio≥90`; citation-velocity ranking; citation-key generation. |
| FR-2.2 | Critic: per-paper extraction (problem/method/dataset/results/limitations) → matrix (JSON+MD) + narrative | ✅ | `critic.py` batched extraction → `MatrixModel` JSON artifact + narrative summary; graceful degradation marks failed rows. |
| FR-2.3 | Analyst: sandboxed Python execution | ⛔ Not implemented (correctly) | `NotImplementedError`, v0.2 per BRD §8. |
| FR-2.4 | Scribe: section-by-section RAG prose, BibTeX, MD/LaTeX, **cite-only-from-pool** | ✅ (MD only) | Per-section drafting via RAG; `validate_citations` enforces cited-keys⊆approved-pool with one retry + `INVALID:` flag; LaTeX explicitly errors (v0.2). **Citation invariant (risk #1) is enforced.** |

### 2.3 Orchestration & State Backend (FR-3.x)

| FR | Requirement | Status | Notes |
| --- | --- | --- | --- |
| FR-3.1 | LangGraph workflow engine with enforced gates | ✅ | `graph/workflow.py` — all gates `interrupt()`; approve/reject/override commands; checkpoint persisted before `approval.required`. |
| FR-3.2 | Per-project vector storage (RAG) | ✅ | Chroma, namespaced per project id; URL-scheme whitelist hardening. |
| FR-3.3 | Token/cost logging per LLM call; per-project rollup | ✅ | `audit_log` rows carry model/tokens_in/out/cost_usd; `GET /usage` rollup; real cost from pricing table. |
| FR-3.4 | Persistence: relational store for metadata/state/identity/audit; object storage for files | ⚠️ Partial | Postgres covers projects/runs/papers/artifacts/audit/users + LangGraph checkpoints. **No S3/GCS object storage** — artifacts (incl. manuscript) live as `content TEXT` in Postgres. Acceptable for MVP (no uploads), but diverges from BRD §3.4. |
| FR-3.5 | Firebase (Google OAuth) auth; resources scoped by UID | ✅ | Firebase ID-token verify + users-upsert; `_assert_owned` on every project route; prod-safe `DEV_AUTH_BYPASS` guard. |

---

## 3. Non-Functional Requirements

| NFR | Requirement | Status | Notes |
| --- | --- | --- | --- |
| NFR-1 | Modularity (FE/BE decoupled via REST+WS) | ✅ | Clean REST + WS contract; frontend talks only to documented endpoints. |
| NFR-2 | Streaming feedback, P95 TTFT ≤ 3s | ⚠️ Mostly | `agent.token` deltas stream into the live log; WS reconnect with backoff. **TTFT not measured** — no perf assertion/telemetry to prove ≤3s. |
| NFR-3 | Per-user data isolation; zero-data-retention LLM config | ⚠️ Partial | Per-project Chroma namespacing + per-user scoping ✓. **Zero-data-retention provider config is not codified** (depends on the API key's account settings; no assertion). |
| NFR-4 | Reproducibility; version-controlled prompt templates | ✅ | Prompt templates are module-level constants under version control; deterministic dedup/ranking. |
| NFR-5 | Per-project cost cap, halt at 80% | ✅ | `_enforce_cost_cap`: warns at `token_cap_warn_pct` (0.8), halts (run→error) at cap; wired into Critic + Scribe gates and the discovery gate. |
| NFR-6 | Structured JSON logs; **trace_id** linking UI→API→LLM | ⚠️ Partial | `structlog` JSON logs with structured event fields ✓. **`trace_id` is still not wired** (carried from Phase-1 §5.2) — no request-scoped trace id threading. |
| NFR-7 | WCAG 2.1 AA for dashboard + approval panels | ⚠️ Partial | Focus-visible rings, reduced-motion, semantic landmarks, labels, some aria present. **No formal WCAG AA audit**; PhaseTracker and Markdown components carry no aria; contrast not formally verified. |

---

## 4. SPEC contract drift

| SPEC ref | Contract | Build | Severity |
| --- | --- | --- | --- |
| §3.5 | `GET /export?format=markdown\|latex\|bibtex` returns the manuscript | **501 stub** despite manuscript being assembled & client-side downloadable | **Major** — the spec'd export path is unreachable; only the UI Blob-download works. |
| §3.7 | Errors return `{ "error": { code, message, trace_id } }` | FastAPI default `{ "detail": { code, message } }` (no `error` wrapper, no `trace_id`) | **Minor** — clients keying on `error.code` would break. |
| §3.4 | `POST /papers/upload` returns extracted metadata + new Paper | 501 (MVP out-of-scope per BRD §8) | Acceptable. |
| §2.3 / §3.4 | Object storage (S3/GCS) for artifacts/PDFs | Artifacts stored as Postgres TEXT | Acceptable for MVP; revisit for v1.0. |
| §4.1 | `usage.tick` periodic rollup event | Event typed in `ws.ts` but **not emitted** by backend nor consumed by a spend meter | **Minor** — cap events work; periodic tick does not. |

---

## 5. Success metrics readiness (BRD §9)

| Metric | Target | Can we measure it today? |
| --- | --- | --- |
| Time to first draft | ≤ 45 min | ⚠️ No instrumentation — UI timestamps not captured/surfaced. |
| Approval-gate compliance | 100% | ✅ Enforced at state-machine level + audit-log assertions in tests. |
| Citation accuracy | ≥ 95% resolve to pool | ✅ Scribe validator + assembler reference resolver enforce this structurally. |
| Time saved vs manual | ≥ 60% | ⛔ Requires post-use survey (out of code scope). |
| Cost per lit review | ≤ USD 5 | ✅ Cost cap defaults to $5 and is enforced; real cost is now tracked. |

---

## 6. Recommendations — what to do to fully satisfy the BRD

### P0 — close the spec-contract gaps (small, high-value)
1. **Implement `GET /export`** (SPEC §3.5). The manuscript artifact already exists; wire `format=markdown` to return it, `format=bibtex` to emit the approved-pool BibTeX, and keep `latex` as an explicit 501/deferred. ~Half a day. Closes the most visible Major drift.
2. **Standardize the error envelope** (SPEC §3.7) — add an exception handler that wraps `HTTPException.detail` into `{ "error": { code, message, trace_id } }`. ~1–2 hours.

### P1 — observability + dashboard completeness
3. **Wire `trace_id`** (NFR-6) — request-scoped middleware injecting a UUID into structlog contextvars + the error envelope; thread it through agent/LLM log calls. Closes the longest-standing carried finding.
4. **Persistent spend meter in the dashboard** (FR-1.1) — fetch `/usage` and/or consume a `usage.tick` event; render a running "$X.XX / $5.00" indicator in the nav rail. Emit `usage.tick` from the backend.

### P2 — UI completeness (nice-to-have for faculty-acceptance)
5. **Inline BibTeX preview + citation-key editor** (FR-1.5) — a small citations panel that shows each approved paper's BibTeX and lets the user fix a malformed key before approving.
6. **Formal WCAG 2.1 AA pass** (NFR-7) — add aria to PhaseTracker/Markdown, run an axe-core audit, verify contrast on the pure-black theme.

### P3 — deferred-by-design (confirm these stay v0.2/v0.3)
7. Object storage (FR-3.4) — move artifacts/manuscripts to S3/GCS for v1.0.
8. Analyst/Phase 3, browser automation, LaTeX, multi-LLM fallback — all correctly deferred; no action for MVP.

---

## 7. Bottom line

The Phase-4 codebase **satisfies the MVP (v0.1) scope defined in BRD §8** and enforces the non-negotiable HITL contract (BRD §1, §4) at the state-machine level — the system's defining requirement. The four Phase-1 blockers and the major Phase-1 findings (users-upsert, gate wiring) are all closed. 209/209 backend tests pass.

To call it **fully BRD-compliant against the documented MVP contract**, the highest-leverage work is: (1) implement `/export`, (2) fix the error envelope, (3) wire `trace_id`, (4) add the dashboard spend meter. Items 1–2 are a day; 3–4 a day or two. Everything else is either correctly deferred to v0.2/v0.3 or polish.

— End of report —
