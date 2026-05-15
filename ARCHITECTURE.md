# Architecture — ResearchFlow AI

This document explains *why* the system is structured the way it is. For the *what* (data models, endpoints, events), read [`SPEC.md`](./SPEC.md). For the *what we're building and why*, read [`BRD.md`](./BRD.md).

---

## 1. Architectural principles

1. **HITL is a property of the state machine, not the UI.** A misbehaving client must not be able to bypass approval gates. The LangGraph engine itself refuses to advance without an authenticated approval event.
2. **The local client owns user-identity work.** Browser automation and PDF parsing run on the user's machine — partly for privacy (PDFs may be unpublished), partly because publisher sites block cloud IPs.
3. **Specs are the contract.** REST + WS contracts and agent I/O signatures are authored *before* code. Implementation that drifts from the spec is rejected at review.
4. **Pluggable providers.** LLM provider, vector store, and storage are abstractions. The default implementation choices in v0.1 are not load-bearing.
5. **Auditable by default.** Every agent invocation, user action, and model call writes to an append-only audit log. There is no "fast path" that skips logging.

---

## 2. High-level component map

```
                           ┌───────────────────────────────────────┐
                           │           Local Client (Next.js)        │
                           │                                         │
   user ──────────────────►│  Dashboard · Approval Panels · PDF       │
                           │  parser · Playwright agent              │
                           └───┬──────────────────────┬──────────────┘
                               │ REST (control)       │ WS (events / streaming)
                               ▼                      ▼
                           ┌───────────────────────────────────────┐
                           │        FastAPI Remote Engine            │
                           │                                         │
                           │  ┌──────────────────┐                    │
                           │  │  HTTP routes     │   Auth (Firebase)   │
                           │  │  /projects, /wf  │                    │
                           │  └──────┬───────────┘                    │
                           │         │                                │
                           │  ┌──────▼───────────┐                    │
                           │  │  LangGraph engine │  ◄── checkpoints  │
                           │  └──────┬───────────┘                    │
                           │         │                                │
                           │  ┌──────▼──────────────────────────┐    │
                           │  │  Agents (Librarian/Critic/...) │    │
                           │  └──────┬─────────┬───────┬───────┘    │
                           └─────────┼─────────┼───────┼─────────────┘
                                     │         │       │
                                     ▼         ▼       ▼
                                 LLM API   Vector DB  Postgres
                                 (gateway)  (Chroma)  (state + audit)
                                                  │
                                                  ▼
                                            Object store
                                            (PDFs, artifacts)
```

---

## 3. Why hybrid client/server?

| Concern | Where it lives | Why |
| --- | --- | --- |
| LLM inference | Server | Cost, key safety, parallelism. |
| LangGraph state | Server | Single source of truth; survives client reloads. |
| Vector store | Server | Shared embeddings across sessions; large memory footprint. |
| Browser automation | Client | Publisher sites block cloud-IP scrapers; user supervises captchas. |
| PDF parsing | Client | Source PDFs may be unpublished and should not leave the user's machine raw. Only embeddings are uploaded. |
| Approval UI | Client | Latency-sensitive; should feel native. |

This is the single most important architectural call: it is *not* a "thin client" pattern. The client has real responsibilities and runs real automation. The server is the orchestrator and the brain.

---

## 4. State management — the LangGraph engine

The workflow is a directed graph of nodes (agents) and gates (approval barriers). State transitions are owned by LangGraph; the FastAPI layer is a thin adapter between HTTP/WS and the graph.

### Why LangGraph
- Native support for **interrupts**: a node can pause and wait for an external resume event — exactly the HITL contract.
- **Checkpointing** is built-in. A user can close their laptop and resume mid-phase.
- **Cyclical** graphs handle the "reject and regenerate" loop without a custom scheduler.

### Persistence
- Checkpoints → Postgres via `langgraph-checkpoint-postgres`.
- A `workflow_runs` row mirrors the LangGraph thread for application-level queries that don't need to deserialize the checkpoint.

### Critical invariant
The engine refuses to consume an `approve` event for a `WorkflowRun` whose state is not `awaiting_approval`. This is enforced both at the LangGraph layer (the interrupt is the only resumable point) and the application layer (the `/approve` endpoint asserts the state). Belt-and-suspenders.

---

## 5. Data flow — one project, end to end

1. **User creates project.** Backend writes a `projects` row; no workflow run yet.
2. **User starts workflow.** Backend creates a `workflow_runs` row in state `running`, phase `discovery`; LangGraph runs the `discover` node.
3. **Librarian fetches candidates** from Semantic Scholar + ArXiv, writes them to `papers`, transitions the run to `awaiting_approval`, and emits `approval.required` over WS.
4. **User reviews candidates** in the approval panel; toggles `approved` flags via PATCH calls.
5. **User clicks Approve.** Backend calls `langgraph.command(approve)`; the graph advances to `synthesize`.
6. **Critic loads approved papers**, embeds those not yet embedded into the vector store, generates a matrix and a summary, writes both as `artifacts`, transitions to `awaiting_approval`.
7. **User edits the summary inline → clicks Save → clicks Approve.** Save calls the `override` endpoint; the edited artifact replaces the Critic's output with `produced_by: "human"`. Approve advances to `drafting`.
8. **Scribe drafts the Introduction**, citing only from the approved pool. Validator checks every citation key against the pool; if any are unknown, the section is regenerated automatically before reaching the user.
9. **User approves Introduction.** Graph re-enters `draft_section` for the next section.
10. **... repeat for every section ...**
11. **Final assemble** stitches sections + bibliography into a single manuscript artifact; user can `GET /export` to download.

Throughout, every agent call writes one or more `audit_log` rows. The `/audit` endpoint and the dashboard surface this trail.

---

## 6. Cross-cutting concerns

### 6.1 Authentication
Firebase ID tokens on every request. The backend verifies the token using the Firebase Admin SDK and resolves it to a `users.id`. Tokens carry the user's UID; project access checks `projects.owner_id == user.id`.

### 6.2 Authorization
v1 is single-owner per project — no sharing. The authorization check is uniform: "is this project owned by the authenticated user?". A future ACL layer plugs in via a `ProjectPolicy` adapter.

### 6.3 Streaming
LLM token streaming arrives at FastAPI as an async generator. The WS endpoint forwards raw delta strings as `agent.token` events. The client buffers and renders into the UI. Backpressure is handled by an asyncio queue per WS connection with a bounded size — slow clients are disconnected rather than allowed to grow the queue.

### 6.4 Observability
- **Logs:** structured JSON via `structlog`. Every record carries `trace_id`, `project_id`, `workflow_run_id` where applicable.
- **Metrics:** OpenTelemetry spans for HTTP requests, agent invocations, and LLM calls. Exporter is pluggable; default is OTLP-HTTP to a local collector in dev.
- **Cost telemetry:** Every LLM call records tokens + cost into `audit_log`. The `/usage` endpoint rolls these up.

### 6.5 Security
- Secrets via env vars; never committed. Production uses a secrets manager (AWS Secrets Manager / GCP Secret Manager).
- LLM provider configured for zero data retention where the provider supports it.
- Uploaded PDFs are stored in per-user prefixes; access is mediated by signed URLs with short TTLs.
- The Analyst sandbox (v0.2) runs in a process with disabled network, restricted filesystem, and CPU/memory caps.

---

## 7. Deployment topology

### Development
Docker Compose brings up: `postgres`, `chroma`, `backend`, `frontend`. The browser-automation agent runs on the host (Playwright in the developer's local environment).

### Production (target shape, AWS)
- **Frontend:** Vercel (or S3 + CloudFront for a static export). The Playwright agent remains local — production frontend documents how users install/run it.
- **Backend:** ECS Fargate behind an ALB. Two service families: `api` (HTTP/WS) and `worker` (heavy LangGraph runs). Both run from the same container image.
- **Postgres:** RDS.
- **Vector store:** Pinecone (managed) or self-hosted Qdrant on EKS — TBD pending the open question in BRD §14.
- **Object store:** S3, with KMS at rest and per-user prefixes.
- **Auth:** Firebase Auth (managed).
- **Secrets:** AWS Secrets Manager.

GCP equivalents: Cloud Run / Cloud SQL / Cloud Storage / Secret Manager.

---

## 8. Failure modes & their containment

| Failure | Containment |
| --- | --- |
| LLM provider 5xx | Provider abstraction retries with backoff; on persistent failure, the agent node fails the run with a recoverable error and the UI offers a "retry" affordance. |
| LangGraph checkpoint corruption | Each run carries a `started_at` and last-known-good checkpoint; admin tool can roll back to it. |
| Token cap reached mid-phase | Engine pauses with `token_cap_reached`; user must lift the cap to continue. |
| Citation validator rejects Scribe output | One automatic regeneration with the validator error injected as feedback; if it fails twice, surface to the user. |
| Vector store unavailable | Critic and Scribe fail fast with `provider_error`; user sees a clear banner; the workflow run state is preserved. |
| Frontend crash mid-phase | No data loss — all canonical state is server-side. User reloads; the dashboard re-hydrates from `/projects/{id}/workflow`. |

---

## 9. What we are explicitly *not* doing (yet)

- **Realtime multi-user editing.** Single-owner projects only in v1.
- **Custom fine-tuned models.** All inference goes through public APIs in v1.
- **An autonomous mode.** Removing approval gates is out of scope — and would violate the project's stated principles.
- **Mobile / tablet UI.** Desktop-first.
- **Reviewer / publisher integration.** Manuscript export is plain Markdown/LaTeX + BibTeX; users handle submission manually.

Reopen any of these only after the v1.0 milestone.
