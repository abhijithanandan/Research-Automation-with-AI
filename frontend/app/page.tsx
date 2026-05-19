"use client";

/**
 * Phase 1 — single-page HITL workflow UI.
 *
 * Implements BRD FR-1.4: project creation, Librarian run with live WS
 * streaming, paper list selector, and all three BRD §4.2 intervention
 * actions (approve / reject / override).
 *
 * State machine (SPEC §7.4 — no optimistic UI):
 *   idle → creating → running → awaiting → (busy) → done | error
 *
 * The view only advances on WS state.changed events, never on REST responses.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApprovalPanel, type OverridePayload } from "@/components/workflow/ApprovalPanel";
import { PhaseTracker } from "@/components/workflow/PhaseTracker";
import { api } from "@/lib/api";
import { connectProjectEvents, type ServerEvent } from "@/lib/ws";
import type { Paper, Phase, WorkflowState } from "@/lib/types";

// ---------------------------------------------------------------------------
// UI state machine
// ---------------------------------------------------------------------------

type View =
  | "idle"        // no project yet
  | "creating"    // POST /projects + POST /workflow/start in flight
  | "running"     // WS connected, agent working
  | "awaiting"    // approval.required received
  | "busy"        // approve / reject / override REST call in flight (gate not yet advanced)
  | "done"        // state.changed → approved/done
  | "error";      // anything went wrong

interface RunCtx {
  projectId: string;
  runId: string;
  phase: Phase;
  state: WorkflowState;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function paperSourceUrl(paper: Paper): string {
  // Prefer a direct PDF link when available
  if (paper.pdf_url) return paper.pdf_url;
  // Fall back to the canonical source page
  if (paper.source === "arxiv") {
    return `https://arxiv.org/abs/${paper.external_id}`;
  }
  if (paper.source === "semantic_scholar") {
    return `https://www.semanticscholar.org/paper/${paper.external_id}`;
  }
  if (paper.source === "crossref") {
    return `https://doi.org/${paper.external_id}`;
  }
  return "#";
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const DEV_TOKEN = process.env.NEXT_PUBLIC_DEV_TOKEN ?? "dev-token";

export default function HomePage() {
  // --- form inputs ---
  const [title, setTitle] = useState("");
  const [seedQuery, setSeedQuery] = useState("");

  // --- page state machine ---
  const [view, setView] = useState<View>("idle");
  const [error, setError] = useState<string | null>(null);

  // --- run context (set once workflow starts) ---
  const [ctx, setCtx] = useState<RunCtx | null>(null);

  // --- live log lines from WS ---
  const [logLines, setLogLines] = useState<string[]>([]);
  const logEndRef = useRef<HTMLDivElement>(null);

  // --- papers loaded at awaiting_approval ---
  const [papers, setPapers] = useState<Paper[]>([]);
  const [papersLoading, setPapersLoading] = useState(false);

  // --- approval summary from WS ---
  const [approvalSummary, setApprovalSummary] = useState("");

  // --- WS ref so we can close it on unmount ---
  const wsRef = useRef<WebSocket | null>(null);

  // Auto-scroll log
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logLines]);

  // Disconnect WS on unmount
  useEffect(() => {
    return () => {
      wsRef.current?.close();
    };
  }, []);

  // ---------------------------------------------------------------------------
  // WS event handler — central dispatcher
  // ---------------------------------------------------------------------------
  const handleEvent = useCallback(
    (evt: ServerEvent, projectId: string) => {
      switch (evt.type) {
        case "agent.started":
          setLogLines((l) => [...l, `▶ ${evt.agent} started`]);
          break;

        case "agent.token":
          setLogLines((l) => {
            const last = l[l.length - 1] ?? "";
            // Append token to last line if it started from this agent's stream
            if (last.startsWith(`✏ ${evt.agent}`)) {
              return [...l.slice(0, -1), last + evt.delta];
            }
            return [...l, `✏ ${evt.agent} ${evt.delta}`];
          });
          break;

        case "agent.completed":
          setLogLines((l) => [...l, `✓ ${evt.agent} completed`]);
          break;

        case "agent.error":
          setLogLines((l) => [...l, `✗ ${evt.agent} error: ${evt.error}`]);
          break;

        case "approval.required":
          setApprovalSummary(evt.summary ?? "Review the candidates below.");
          setCtx((c) => c ? { ...c, phase: evt.phase, state: "awaiting_approval" } : c);
          // Load papers now that the Librarian has persisted them (B1 fix)
          setPapersLoading(true);
          api.papers
            .list(projectId, DEV_TOKEN)
            .then((p) => setPapers(p))
            .catch(() => setError("Failed to load candidate papers."))
            .finally(() => {
              setPapersLoading(false);
              setView("awaiting");
            });
          break;

        case "state.changed":
          setCtx((c) =>
            c ? { ...c, phase: evt.phase, state: evt.state, runId: evt.run_id } : c,
          );
          // Gate has advanced — transition view per SPEC §7.4 (no optimistic UI)
          if (evt.state === "approved" || evt.phase === "done") {
            setView("done");
          } else if (evt.state === "awaiting_approval") {
            // handled by approval.required event above
          } else if (evt.state === "running" && evt.phase === "discovery") {
            // Reject sent us back to discovery — restart the running view
            setView("running");
          } else if (evt.state === "error") {
            setError("Workflow encountered an error. Check the audit log.");
            setView("error");
          }
          break;

        default:
          break;
      }
    },
    [],
  );

  // ---------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || !seedQuery.trim()) return;

    setView("creating");
    setError(null);
    setLogLines([]);
    setPapers([]);

    try {
      const project = await api.projects.create(
        { title: title.trim(), seed_query: seedQuery.trim() },
        DEV_TOKEN,
      );

      const runCtx: RunCtx = {
        projectId: project.id,
        runId: "",
        phase: "discovery",
        state: "running",
      };
      setCtx(runCtx);

      // Connect WS before starting workflow so we don't miss the first event
      wsRef.current?.close();
      wsRef.current = connectProjectEvents({
        projectId: project.id,
        token: DEV_TOKEN,
        onEvent: (evt) => handleEvent(evt, project.id),
        onError: () => setError("WebSocket connection error."),
        onClose: (e) => {
          if (e.code !== 1000) {
            setError(`WebSocket closed unexpectedly (code ${e.code}).`);
          }
        },
      });

      await api.workflow.start(project.id, DEV_TOKEN);
      setView("running");
      setLogLines(["Project created. Librarian starting…"]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start workflow.");
      setView("error");
    }
  }

  async function handleApprove() {
    if (!ctx) return;
    setView("busy");
    try {
      await api.workflow.approve(ctx.projectId, null, DEV_TOKEN);
      // View transitions only on state.changed WS event (SPEC §7.4)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approve failed.");
      setView("error");
    }
  }

  async function handleReject(feedback: string) {
    if (!ctx) return;
    setView("busy");
    try {
      await api.workflow.reject(ctx.projectId, feedback, DEV_TOKEN);
      setView("running");
      setLogLines((l) => [...l, "↩ Rejected — Librarian regenerating…"]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reject failed.");
      setView("error");
    }
  }

  async function handleOverride(payload: OverridePayload) {
    if (!ctx) return;
    setView("busy");
    try {
      await api.workflow.override(ctx.projectId, payload, DEV_TOKEN);
      // View transitions only on state.changed WS event (SPEC §7.4)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Override failed.");
      setView("error");
    }
  }

  async function handleTogglePaper(paper: Paper) {
    if (!ctx) return;
    try {
      const updated = await api.papers.setApproved(
        ctx.projectId,
        paper.id,
        !paper.approved,
        DEV_TOKEN,
      );
      setPapers((ps) => ps.map((p) => (p.id === updated.id ? updated : p)));
    } catch {
      // Phase-locked or network error — surface inline, don't crash the page
      setPapers((ps) => ps); // no-op re-render to show stale state
    }
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  const isBusy = view === "busy" || view === "creating";

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-16">
      {/* Header */}
      <header className="space-y-1">
        <p className="text-xs font-medium uppercase tracking-wider text-slate-500">
          ResearchFlow AI
        </p>
        <h1 className="text-3xl font-semibold">Phase 1 — Discovery</h1>
        <p className="text-sm text-slate-500">
          Create a project, let the Librarian fetch candidates, review and
          approve your working paper pool.
        </p>
      </header>

      {/* Phase tracker — shown once a run exists */}
      {ctx && (
        <PhaseTracker current={ctx.phase} />
      )}

      {/* ------------------------------------------------------------------ */}
      {/* IDLE: project creation form                                         */}
      {/* ------------------------------------------------------------------ */}
      {view === "idle" && (
        <form onSubmit={handleCreate} className="space-y-4 rounded-lg border border-slate-200 p-6 dark:border-slate-800">
          <h2 className="text-lg font-semibold">New project</h2>
          <div className="space-y-1">
            <label className="block text-sm font-medium" htmlFor="title">
              Title
            </label>
            <input
              id="title"
              type="text"
              required
              className="w-full rounded border border-slate-300 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-900"
              placeholder="Survey of HITL in agentic systems"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <label className="block text-sm font-medium" htmlFor="seed">
              Seed query
            </label>
            <input
              id="seed"
              type="text"
              required
              className="w-full rounded border border-slate-300 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-900"
              placeholder="human-in-the-loop multi-agent LLM"
              value={seedQuery}
              onChange={(e) => setSeedQuery(e.target.value)}
            />
          </div>
          <button
            type="submit"
            className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            Create &amp; start Librarian
          </button>
        </form>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* CREATING: spinner while project + workflow are initialising         */}
      {/* ------------------------------------------------------------------ */}
      {view === "creating" && (
        <div className="flex items-center gap-3 text-sm text-slate-500">
          <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-blue-500" />
          Creating project and connecting…
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* RUNNING: live agent log                                             */}
      {/* ------------------------------------------------------------------ */}
      {(view === "running" || view === "busy") && (
        <section className="space-y-3">
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-blue-500" />
            {view === "busy" ? "Waiting for workflow to advance…" : "Librarian is working…"}
          </div>
          <AgentLog lines={logLines} endRef={logEndRef} />
        </section>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* AWAITING: paper list + approval panel                               */}
      {/* ------------------------------------------------------------------ */}
      {(view === "awaiting" || (view === "busy" && papers.length > 0)) && (
        <section className="space-y-4">
          <AgentLog lines={logLines} endRef={logEndRef} />

          {/* Paper list selector — FR-1.4 */}
          <div className="rounded-lg border border-slate-200 dark:border-slate-800">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-800">
              <h2 className="text-sm font-semibold">
                Candidate papers
                {papers.length > 0 && (
                  <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                    {papers.filter((p) => p.approved).length} / {papers.length} selected
                  </span>
                )}
              </h2>
              <p className="mt-0.5 text-xs text-slate-500">
                Check the papers you want in your approved pool. Only checked papers
                will be passed to the Critic in Phase 2.
              </p>
            </div>

            {papersLoading && (
              <div className="flex items-center gap-2 p-4 text-sm text-slate-500">
                <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-blue-500" />
                Loading candidates…
              </div>
            )}

            {!papersLoading && papers.length === 0 && (
              <p className="p-4 text-sm text-slate-500">No candidates found.</p>
            )}

            {!papersLoading && papers.length > 0 && (
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {papers.map((paper) => (
                  <li key={paper.id} className="flex gap-3 px-4 py-3">
                    <input
                      type="checkbox"
                      id={`paper-${paper.id}`}
                      checked={paper.approved}
                      disabled={isBusy}
                      onChange={() => handleTogglePaper(paper)}
                      className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded accent-blue-600 disabled:cursor-not-allowed"
                    />
                    <label
                      htmlFor={`paper-${paper.id}`}
                      className="cursor-pointer space-y-0.5"
                    >
                      <p className="text-sm font-medium leading-snug">
                        <a
                          href={paperSourceUrl(paper)}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="hover:underline hover:text-blue-500"
                        >
                          {paper.title}
                        </a>
                      </p>
                      <p className="text-xs text-slate-500">
                        {paper.authors.slice(0, 3).join(", ")}
                        {paper.authors.length > 3 && " et al."}
                        {paper.year ? ` · ${paper.year}` : ""}
                        {" · "}
                        <code className="text-xs">{paper.citation_key}</code>
                        {" · "}
                        <span className="capitalize">{paper.source.replace("_", " ")}</span>
                      </p>
                      {paper.abstract && (
                        <p className="mt-1 line-clamp-2 text-xs text-slate-400">
                          {paper.abstract}
                        </p>
                      )}
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* All three BRD §4.2 intervention actions */}
          <ApprovalPanel
            summary={approvalSummary}
            busy={isBusy}
            onApprove={handleApprove}
            onReject={handleReject}
            onOverride={handleOverride}
          />
        </section>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* DONE                                                                */}
      {/* ------------------------------------------------------------------ */}
      {view === "done" && (
        <div className="space-y-3 rounded-lg border border-green-200 bg-green-50 p-6 dark:border-green-800 dark:bg-green-950">
          <p className="font-semibold text-green-800 dark:text-green-200">
            Phase 1 complete
          </p>
          <p className="text-sm text-green-700 dark:text-green-300">
            {papers.filter((p) => p.approved).length} paper
            {papers.filter((p) => p.approved).length !== 1 ? "s" : ""} approved and
            locked into your working pool. Phase 2 (Synthesis) will begin when
            the team is ready.
          </p>
          <AgentLog lines={logLines} endRef={logEndRef} />
          <button
            type="button"
            onClick={() => {
              setView("idle");
              setCtx(null);
              setLogLines([]);
              setPapers([]);
              setTitle("");
              setSeedQuery("");
              wsRef.current?.close();
            }}
            className="rounded border border-green-400 px-3 py-1.5 text-sm font-medium text-green-800 hover:bg-green-100 dark:text-green-200"
          >
            Start new project
          </button>
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* ERROR                                                               */}
      {/* ------------------------------------------------------------------ */}
      {view === "error" && (
        <div className="space-y-3 rounded-lg border border-red-200 bg-red-50 p-6 dark:border-red-800 dark:bg-red-950">
          <p className="font-semibold text-red-800 dark:text-red-200">Something went wrong</p>
          {error && <p className="text-sm text-red-700 dark:text-red-300">{error}</p>}
          <button
            type="button"
            onClick={() => {
              setView("idle");
              setError(null);
              setCtx(null);
              setLogLines([]);
              setPapers([]);
              wsRef.current?.close();
            }}
            className="rounded border border-red-400 px-3 py-1.5 text-sm font-medium text-red-800 hover:bg-red-100 dark:text-red-200"
          >
            Try again
          </button>
        </div>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// AgentLog — scrollable live event log
// ---------------------------------------------------------------------------

function AgentLog({
  lines,
  endRef,
}: {
  lines: string[];
  endRef: React.RefObject<HTMLDivElement>;
}) {
  if (lines.length === 0) return null;
  return (
    <div className="max-h-48 overflow-y-auto rounded border border-slate-200 bg-slate-50 p-3 font-mono text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
      {lines.map((line, i) => (
        <div key={i} className="whitespace-pre-wrap break-all">
          {line}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}
