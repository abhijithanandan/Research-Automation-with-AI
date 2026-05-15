# Business and Functional Requirements Document (BRD/FRD)

**Project Name:** ResearchFlow AI — Agentic Research Automation System
**Document Version:** 1.1 (Draft for Engineering Onboarding)
**Owner:** Abhijith Anandakrishnan (Amrita)
**Target Audience:** Development & Engineering Team, Faculty Reviewers
**Status:** Approved for build-out of MVP

---

## 1. Executive Summary

ResearchFlow AI is a hybrid, multi-agent AI workflow designed to accelerate academic and technical research. By automating repetitive tasks across the research lifecycle — from literature discovery through manuscript drafting — the system reduces the manual compilation burden on students and researchers while keeping a human firmly in control of every consequential decision.

A defining operational principle is **Strict Human-in-the-Loop (HITL) Orchestration**: the AI acts as a co-pilot, not an autonomous replacement. The workflow engine *must* pause and request explicit human approval ("green light") before transitioning between phases of the research workflow. This is non-negotiable for academic integrity and is enforced at the state-machine level, not merely the UI level.

### 1.1 Problem Statement

Research students typically spend 40–60% of their early project time on mechanical tasks: searching databases, deduplicating papers, building comparison matrices, formatting citations, and reformatting draft prose. Existing tools (Zotero, Elicit, ChatGPT) solve isolated slices but do not orchestrate the workflow end-to-end, and fully-autonomous "AI researchers" violate academic integrity norms by removing the human from authorship.

### 1.2 Solution Summary

A multi-agent system organized around four specialist personas (Librarian, Critic, Analyst, Scribe), orchestrated by a graph-based state machine with mandatory approval gates between every phase. The system runs as a hybrid client/server application: a local Next.js client owns the UI and any operation that requires the user's IP/identity (browser automation, local PDF parsing), while a cloud FastAPI backend handles inference, vector retrieval, and sandboxed code execution.

### 1.3 Goals & Non-Goals

**Goals**
- Reduce time-to-first-draft for a literature review by ≥60% versus a manual baseline.
- Keep the human author in the decision loop at every phase transition (HITL).
- Produce verifiable, citation-traceable outputs (every claim links to a source in the approved pool).
- Support both Markdown and LaTeX manuscript output.

**Non-Goals (MVP)**
- Autonomous "press-and-walk-away" mode. The system must not run more than one phase without human approval.
- Multi-author real-time collaboration (single-author projects only in v1).
- Mobile or tablet UI.
- Reviewer-facing or publisher submission integrations.
- Hosting fine-tuned in-house models. The system uses third-party LLM APIs in v1.

---

## 2. Personas & Primary Use Cases

### 2.1 Primary Personas

| Persona | Description | Top Need |
| --- | --- | --- |
| **Postgrad / PhD student** | Running their first or second literature review; comfortable with technical tools. | Speed and citation traceability. |
| **Undergraduate researcher** | Working on a final-year project under faculty supervision. | Guided structure and faculty-acceptable output. |
| **Faculty / advisor** | Supervising students; needs visibility into the AI's role. | Audit trail of what was AI-generated vs human-edited. |

### 2.2 Primary Use Cases

1. **Literature review on a new topic** — user supplies seed keywords; system returns a curated paper pool, a comparison matrix, and a draft "Related Work" section.
2. **Resume an in-progress draft** — user uploads existing notes and PDFs; system embeds them, surfaces gaps, and continues drafting from the current section.
3. **Data analysis appendix** — user provides a dataset; the Analyst agent generates exploratory plots and a methods description, gated by code review.

---

## 3. System Architecture Overview

To balance heavy computational requirements with local browser accessibility and data security, the system employs a **Hybrid Client-Server Architecture**.

* **Local Node (User-Facing Client):** Deployed on the user's machine. Owns the UI, workflow visualization, local PDF parsing, and the browser-automation agent. Browser automation runs locally so the user's residential IP is used — avoiding the cloud-IP bot detection that blocks scraping from datacenters.
* **Remote Engine (Cloud Server):** Handles LLM inference, vector embeddings, long-running summarization, code execution sandboxes, and durable state persistence.
* **Communication:** REST for command/control, WebSocket for streaming agent tokens and live state updates.

A detailed architecture write-up — including data-flow diagrams and deployment topology — lives in `ARCHITECTURE.md`.

---

## 4. Human-in-the-Loop (HITL) Workflow & State Management

The core workflow is modeled as a directed state machine implemented with LangGraph. State transitions across phase boundaries are *guarded* — the graph cannot advance without an explicit `approve` event from an authenticated user.

### 4.1 Phase Gates

The pipeline is divided into four primary phases. The engine halts execution at the end of each phase, persists the state, and surfaces a review payload to the UI.

1. **Phase 1 — Query & Discovery:** Librarian fetches candidate papers from APIs and (where needed) the local browser agent. *System pauses.* User reviews, selects/deselects, and approves the working pool.
2. **Phase 2 — Literature Synthesis:** Critic reads the approved pool and produces a structured review matrix plus narrative summary. *System pauses.* User edits, requests regeneration with feedback, or approves.
3. **Phase 3 — Data / Experiment Analysis (optional):** Analyst writes and executes code in a sandbox to produce figures, tables, and a methods narrative. *System pauses.* User reviews the generated artifacts and code log.
4. **Phase 4 — Drafting:** Scribe writes the manuscript one section at a time (Abstract → Introduction → Related Work → Methodology → Results → Discussion → Conclusion). *System pauses after every section.* User edits, prompts revisions, or approves before the next section starts.

### 4.2 Intervention Actions

At any paused state, the user can:

* **Approve & Proceed** — green light; the graph advances.
* **Reject & Regenerate** — supply a free-text instruction; the current node re-runs.
* **Manual Override** — edit the generated artifact directly; the edited version becomes the new ground truth and is recorded as `human_edited` in the audit trail.
* **Branch** *(post-MVP)* — fork the state and explore an alternative path without losing the current draft.

### 4.3 Audit Trail

Every node output, user action, model name, prompt template version, and token count is appended to a per-project audit log. This log is exportable and is the basis for the "AI disclosure appendix" that students can attach to submissions.

---

## 5. Functional Requirements

### 5.1 Local Client Module

* **FR-1.1 Dashboard:** A visual interface tracking project state — completed phases, current phase, pending approvals, agent activity, and token spend.
* **FR-1.2 Local Document Parser:** Upload and parse local PDFs, extracting text, figures, and metadata. Extracted text is chunked and embedded; embeddings are pushed to the cloud vector store.
* **FR-1.3 Browser Automation Agent:** A locally-running Playwright instance that receives URL targets and selector hints from the Remote Engine, navigates academic sites, handles simple login flows (user-supervised), and scrapes text/PDFs. Must run with the user watching — never headlessly without consent.
* **FR-1.4 Approval UI:** A dedicated review panel for each phase — paper list selector for Phase 1, diff/edit view for Phases 2 and 4, plot/code viewer for Phase 3.
* **FR-1.5 Citation Manager:** Inline BibTeX preview; ability to manually correct any malformed citation before approving a section.

### 5.2 Core Agentic Personas

* **FR-2.1 The Librarian (Discovery):** Integrates with Semantic Scholar, ArXiv, and Crossref. Expands a seed query with synonyms and related terms. Deduplicates by DOI and title-fuzzy-match. Returns ranked candidates with abstracts and metadata.
* **FR-2.2 The Critic (Reviewer):** Reads the approved paper pool. Extracts (per paper): problem, method, dataset, key results, limitations. Produces a comparative matrix (structured JSON + rendered Markdown table) and a narrative synthesis.
* **FR-2.3 The Analyst (Compute):** A sandboxed Python execution environment (process-isolated, network-restricted, time-limited). Receives a task description and a dataset reference; produces code, executes it, and returns figures, tables, and stdout. Code is *always* shown to the user before execution.
* **FR-2.4 The Scribe (Writer):** Generates academic prose section by section, drawing context from the approved literature pool via RAG. Supports BibTeX integration and outputs Markdown (default) or LaTeX. Every factual claim must include at least one citation key from the approved pool.

### 5.3 Orchestration & State Backend

* **FR-3.1 Workflow Engine:** LangGraph-based state manager that owns the canonical project state and enforces approval gates.
* **FR-3.2 Vector Storage:** RAG backend serving as context for the Critic and Scribe. Per-project namespacing.
* **FR-3.3 Token/Cost Management:** Logs every LLM call with model, token counts, and cost estimate. Per-project and per-user rollups exposed via the dashboard.
* **FR-3.4 Persistence:** Project metadata, workflow state, user identity, and audit logs in a relational store. Generated artifacts and uploaded files in object storage.
* **FR-3.5 Authentication:** OAuth (Google) via Firebase Auth in v1. Project resources scoped by user UID.

---

## 6. Non-Functional Requirements

* **NFR-1 Modularity:** Frontend and backend are decoupled, communicating only via documented REST endpoints and WebSocket events (see `SPEC.md`).
* **NFR-2 Latency & Feedback:** Long-running tasks must stream progress (tokens or progress events) so the UI never appears frozen. P95 time-to-first-token ≤ 3s for streaming endpoints.
* **NFR-3 Security & Privacy:** Uploaded unpublished data is namespaced per user and never used for training. LLM providers must be configured for zero-data-retention.
* **NFR-4 Reproducibility:** Given the same approved inputs and the same model snapshot, agents should produce semantically equivalent outputs. Prompt templates are version-controlled.
* **NFR-5 Cost Cap:** Per-project token spend has a configurable cap. The system halts and prompts the user when 80% is reached.
* **NFR-6 Observability:** Structured logs (JSON) for every agent invocation. Trace IDs link UI actions → API calls → LLM calls.
* **NFR-7 Accessibility:** UI must meet WCAG 2.1 AA for the dashboard and approval panels.

---

## 7. Proposed Technology Stack

**Frontend / Local Client**
* **Framework:** Next.js 14 (App Router) with React 18 and TypeScript.
* **Styling:** Tailwind CSS + shadcn/ui primitives.
* **State:** Zustand for client-local state; server state via TanStack Query.
* **Local Automation:** Playwright for browser-use tasks.
* **Future:** Tauri wrapper for a packaged desktop build.

**Backend / Remote Engine**
* **Language:** Python 3.11+.
* **API Framework:** FastAPI (REST + WebSocket).
* **Agent Framework:** LangGraph for cyclical graphs and HITL pause semantics. LangChain for tool/model abstractions where helpful.
* **Sandbox:** Subprocess + resource limits in v1; containerized (gVisor or Firecracker) in v2.

**Infrastructure & Data**
* **Cloud:** AWS (default) or GCP. Containerized via Docker; orchestrated with Docker Compose in dev, ECS/Cloud Run in prod.
* **Vector DB:** Chroma (self-hosted) for dev/MVP; Pinecone or Qdrant for production scale.
* **Relational DB:** PostgreSQL (project metadata, workflow state, audit log).
* **Object Storage:** S3 / GCS for uploaded PDFs and generated artifacts.
* **Auth:** Firebase Auth (Google OAuth).
* **LLM Providers:** Pluggable. Default to Gemini 2.5 Pro; secondary support for OpenAI, Anthropic Claude, and DeepSeek via a common abstraction layer.

---

## 8. MVP Scope (v0.1)

The MVP is intentionally narrow to validate the HITL contract and architecture end-to-end.

**In scope**
- Single-user, single-project mode.
- Phases 1, 2, and 4 (skip the Analyst / Phase 3 in MVP).
- Semantic Scholar + ArXiv integrations (skip browser-use scraping in MVP).
- Markdown manuscript output (LaTeX in v0.2).
- Chroma local vector store; Postgres on Docker Compose; one LLM provider.

**Out of scope for MVP**
- Phase 3 (Analyst).
- Browser-use scraping.
- LaTeX output.
- Multi-LLM fallback.
- Multi-project dashboard.

---

## 9. Success Metrics

| Metric | Target (MVP) | Measurement |
| --- | --- | --- |
| Time to first usable lit-review draft | ≤ 45 minutes (from project creation) | UI timestamps |
| Approval-gate compliance | 100% (no phase advances without an `approve` event) | Audit log assertion |
| Citation accuracy | ≥ 95% of citations resolve to a paper in the approved pool | Automated check |
| User-reported time saved vs manual | ≥ 60% | Post-use survey |
| Cost per completed lit review | ≤ USD 5 in LLM tokens | Token-spend log |

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| LLM hallucinated citations | High | High | Constrain Scribe to cite-only-from-approved-pool; post-generation validator rejects unknown citation keys. |
| Academic integrity concerns from faculty | Medium | High | Built-in audit trail and exportable AI-disclosure appendix; clear UI labelling of AI-generated vs human-edited spans. |
| Bot-detection blocking from publisher sites | Medium | Medium | Local browser automation uses user's IP; rate-limit; fall back to API-only sources. |
| LLM cost spikes | Medium | Medium | Per-project token cap; cheap-model summarization tier before expensive synthesis. |
| Vendor lock-in to one LLM | Low | Medium | Abstraction layer with pluggable providers from day one. |
| Sandbox escape (Analyst) | Low | High | Phase 3 deferred to v0.2 and gated by a security review; user must approve code before execution. |

---

## 11. Assumptions

- Users have stable broadband and a modern laptop capable of running Node.js and Playwright.
- Users have or can obtain at least one LLM API key.
- Target institutions permit the use of third-party LLM APIs for non-sensitive research material.
- v1 is English-only.

---

## 12. Roadmap

| Version | Target | Highlights |
| --- | --- | --- |
| v0.1 (MVP) | Q3 2026 | Phases 1, 2, 4 with Semantic Scholar + ArXiv. Markdown output. Single LLM. |
| v0.2 | Q4 2026 | Analyst (Phase 3) with sandboxed Python. LaTeX output. Multi-LLM fallback. |
| v0.3 | Q1 2027 | Browser-use scraping. Multi-project dashboard. Token-cost dashboard. |
| v1.0 | Q2 2027 | Tauri desktop packaging. Faculty-facing audit-export. Production hardening. |

---

## 13. Glossary

- **HITL** — Human-in-the-Loop. The architectural commitment that every consequential state transition requires explicit human approval.
- **Agent / Persona** — A configured LLM-driven worker with a specific role, prompt template, and tool set.
- **Approval Gate** — A guarded edge in the state graph that requires an `approve` event before advancing.
- **Approved Pool** — The set of papers a user has explicitly selected in Phase 1; the *only* source the Scribe may cite.
- **Audit Log** — The append-only record of every agent output, user action, and model call for a project.
- **RAG** — Retrieval-Augmented Generation. Context-injection from the vector store at inference time.

---

## 14. Open Questions

These are tracked in the team's issue tracker; resolution required before/around MVP cutover.

1. Default LLM provider for MVP — Gemini vs Claude vs DeepSeek? (Cost-vs-quality benchmark needed.)
2. PDF parser library — `pypdf`, `pdfplumber`, or `unstructured`? Will be decided after a 1-day spike.
3. Hosted Postgres provider for production — Supabase, RDS, or Cloud SQL?
4. Storage of the audit log — same Postgres instance, or a separate append-only store?
