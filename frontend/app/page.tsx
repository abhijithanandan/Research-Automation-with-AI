"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApprovalPanel, type OverridePayload } from "@/components/workflow/ApprovalPanel";
import { Markdown } from "@/components/workflow/Markdown";
import { PhaseTracker } from "@/components/workflow/PhaseTracker";
import {
  SynthesisReview,
  type SynthesisOverridePayload,
} from "@/components/workflow/SynthesisReview";
import { api } from "@/lib/api";
import { connectProjectEvents, type ManagedSocket, type ServerEvent } from "@/lib/ws";
import type { Artifact, Paper, Phase, WorkflowState } from "@/lib/types";

// ---------------------------------------------------------------------------
// UI state machine
// ---------------------------------------------------------------------------

type View =
  | "idle"
  | "creating"
  | "running"
  | "awaiting"
  | "synthesis"
  | "busy"
  | "done"
  | "error";

interface RunCtx {
  projectId: string;
  runId: string;
  phase: Phase;
  state: WorkflowState;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// A DOI looks like "10.<registrant>/<suffix>" — detect so we route DOI-keyed
// papers to doi.org (which redirects to whichever publisher owns the record)
// instead of guessing at a source-specific URL pattern that won't resolve.
function looksLikeDOI(external_id: string): boolean {
  return /^10\.\d{4,9}\//.test(external_id);
}

// Some Semantic Scholar entries carry a stale IEEE Xplore PDF URL pointing at
// the deprecated /ielx7/<group>/<issue>/<artnum>.pdf CDN endpoint. IEEE
// retired that CDN — every such URL now 404s. The article number is still
// embedded in the URL, so we rewrite to the working /document/<artnum> page.
const _IELX_RE = /ieeexplore\.ieee\.org\/ielx\d*\/[^/]+\/[^/]+\/0*(\d+)\.pdf/i;

function rewriteIeeeIfBroken(url: string): string | null {
  const m = _IELX_RE.exec(url);
  if (!m) return null;
  const artnum = m[1];
  return `https://ieeexplore.ieee.org/document/${artnum}`;
}

function paperSourceUrl(paper: Paper): string {
  // The pdf_url field is the strongest hint we have — but only when it
  // actually resolves. The IEEE /ielx7/ URLs that Semantic Scholar returns
  // for IEEE papers are universally 404s today; rewrite them to the working
  // /document/<artnum> page instead.
  if (paper.pdf_url) {
    const rewritten = rewriteIeeeIfBroken(paper.pdf_url);
    if (rewritten) return rewritten;
    return paper.pdf_url;
  }
  const id = paper.external_id ?? "";
  // DOI is universal — prefer it over source-specific URL guesses. Semantic
  // Scholar and Crossref typically return a DOI as the external id; CORE may
  // too. Europe PMC returns DOI when known, else a PMC id.
  if (looksLikeDOI(id)) return `https://doi.org/${id}`;
  if (paper.source === "arxiv") return `https://arxiv.org/abs/${id}`;
  if (paper.source === "semantic_scholar")
    return `https://www.semanticscholar.org/paper/${id}`;
  if (paper.source === "europe_pmc" && id.startsWith("PMC"))
    return `https://europepmc.org/article/PMC/${id.slice(3)}`;
  if (paper.source === "core")
    return `https://core.ac.uk/search?q=${encodeURIComponent(paper.title)}`;
  return "#";
}

function sourceLabel(source: string) {
  const map: Record<string, string> = {
    arxiv: "arXiv",
    semantic_scholar: "Semantic Scholar",
    crossref: "Crossref",
    core: "CORE",
    europe_pmc: "Europe PMC",
    upload: "Upload",
  };
  return map[source] ?? source;
}

function sourceBadgeClass(source: string) {
  if (source === "arxiv") return "bg-orange-500/10 text-orange-400 border-orange-500/20";
  if (source === "semantic_scholar") return "bg-blue-500/10 text-blue-400 border-blue-500/20";
  if (source === "core") return "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
  if (source === "europe_pmc") return "bg-pink-500/10 text-pink-400 border-pink-500/20";
  return "bg-slate-700/50 text-slate-400 border-slate-600/50";
}

function latestArtifact(artifacts: Artifact[]): Artifact | null {
  const sorted = [...artifacts].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
  return sorted[0] ?? null;
}

function phaseLabel(phase: Phase): string {
  const map: Record<Phase, string> = {
    discovery: "Phase 1 · Discovery",
    synthesis: "Phase 2 · Synthesis",
    analysis: "Phase 3 · Analysis",
    drafting: "Phase 4 · Drafting",
    done: "Complete",
  };
  return map[phase] ?? phase;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const DEV_TOKEN = process.env.NEXT_PUBLIC_DEV_TOKEN ?? "dev-token";

export default function HomePage() {
  const [title, setTitle] = useState("");
  const [seedQuery, setSeedQuery] = useState("");
  const [view, setView] = useState<View>("idle");
  const [error, setError] = useState<string | null>(null);
  const [ctx, setCtx] = useState<RunCtx | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const logEndRef = useRef<HTMLDivElement>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [papersLoading, setPapersLoading] = useState(false);
  const [approvalSummary, setApprovalSummary] = useState("");
  const [matrix, setMatrix] = useState<Artifact | null>(null);
  const [summary, setSummary] = useState<Artifact | null>(null);
  const [synthesisLoading, setSynthesisLoading] = useState(false);
  // Which phase the DONE screen should report as complete. Set when the
  // workflow finishes a phase — not inferred from ctx.phase, which is unreliable
  // (the backend reports phase="synthesis" even after synthesis is approved).
  const [completedPhase, setCompletedPhase] = useState<"discovery" | "synthesis" | null>(null);
  const wsRef = useRef<ManagedSocket | null>(null);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logLines]);

  useEffect(() => {
    return () => { wsRef.current?.close(); };
  }, []);

  const handleEvent = useCallback((evt: ServerEvent, projectId: string) => {
    switch (evt.type) {
      case "agent.started":
        setLogLines((l) => [...l, `▶  ${evt.agent} started`]);
        break;
      case "agent.token":
        setLogLines((l) => {
          const last = l[l.length - 1] ?? "";
          if (last.startsWith(`✏  ${evt.agent}`)) return [...l.slice(0, -1), last + evt.delta];
          return [...l, `✏  ${evt.agent} ${evt.delta}`];
        });
        break;
      case "agent.completed":
        setLogLines((l) => [...l, `✓  ${evt.agent} completed`]);
        break;
      case "agent.error":
        setLogLines((l) => [...l, `✗  ${evt.agent} error: ${evt.error}`]);
        break;
      case "approval.required":
        setApprovalSummary(evt.summary ?? "Review the output below.");
        setCtx((c) => c ? { ...c, phase: evt.phase, state: "awaiting_approval" } : c);
        if (evt.phase === "synthesis") {
          // Phase 2 — load the Critic's matrix + summary artifacts, plus the
          // paper pool so the matrix can show titles (not just citation keys).
          setSynthesisLoading(true);
          setView("synthesis");
          Promise.all([
            api.artifacts.list(projectId, "matrix", DEV_TOKEN),
            api.artifacts.list(projectId, "summary", DEV_TOKEN),
            api.papers.list(projectId, DEV_TOKEN),
          ])
            .then(([matrices, summaries, paperList]) => {
              setMatrix(latestArtifact(matrices));
              setSummary(latestArtifact(summaries));
              if (paperList.length > 0) setPapers(paperList);
            })
            .catch(() => setError("Failed to load synthesis artifacts."))
            .finally(() => setSynthesisLoading(false));
        } else if (evt.phase === "discovery") {
          // Phase 1 — load the candidate paper pool. Guard against a replayed
          // stale discovery event dragging the user back from a later phase.
          setPapersLoading(true);
          api.papers
            .list(projectId, DEV_TOKEN)
            .then((p) => setPapers(p))
            .catch(() => setError("Failed to load candidate papers."))
            .finally(() => {
              setPapersLoading(false);
              setView((v) =>
                v === "synthesis" || v === "done" ? v : "awaiting",
              );
            });
        }
        break;
      case "state.changed":
        setCtx((c) => c ? { ...c, phase: evt.phase, state: evt.state, runId: evt.run_id } : c);
        // Guarded functional updates: a replayed/late state.changed must never
        // knock the user out of an active approval screen back into "busy".
        if (evt.phase === "done" || evt.phase === "drafting") {
          // Phase 2 approved — drafting begins (Scribe is a Phase 4 stub).
          setCompletedPhase("synthesis");
          setView("done");
        } else if (evt.state === "approved" && evt.phase === "synthesis") {
          // Synthesis approved & graph ran to completion (draft stubs → assemble).
          setCompletedPhase("synthesis");
          setView("done");
        } else if (evt.state === "approved" && evt.phase === "discovery") {
          // Phase 1 approved — synthesis is about to run; show "busy" ONLY while
          // still on a Phase-1 screen. If we are already showing the synthesis
          // review (the approval.required arrived first, or this is a replay),
          // do not regress.
          setView((v) =>
            v === "awaiting" || v === "running" ? "busy" : v,
          );
          setLogLines((l) =>
            l.includes("✓  Pool approved — Critic synthesizing…")
              ? l
              : [...l, "✓  Pool approved — Critic synthesizing…"],
          );
        } else if (evt.state === "awaiting_approval") {
          // handled by approval.required
        } else if (evt.state === "running" && evt.phase === "discovery") {
          setView((v) => (v === "idle" || v === "creating" ? "running" : v));
        } else if (evt.state === "error") {
          setError("Workflow encountered an error.");
          setView("error");
        }
        break;
    }
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || !seedQuery.trim()) return;
    setView("creating");
    setError(null);
    setLogLines([]);
    setPapers([]);
    setMatrix(null);
    setSummary(null);
    setCompletedPhase(null);
    try {
      const project = await api.projects.create(
        { title: title.trim(), seed_query: seedQuery.trim() },
        DEV_TOKEN,
      );
      const runCtx: RunCtx = { projectId: project.id, runId: "", phase: "discovery", state: "running" };
      setCtx(runCtx);
      wsRef.current?.close();
      wsRef.current = connectProjectEvents({
        projectId: project.id,
        token: DEV_TOKEN,
        onEvent: (evt) => handleEvent(evt, project.id),
        onError: () => setError("WebSocket connection error."),
        onClose: (e) => { if (e.code !== 1000) setError(`WebSocket closed (code ${e.code}).`); },
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
      setLogLines((l) => [...l, "↩  Rejected — Librarian regenerating…"]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reject failed.");
      setView("error");
    }
  }

  async function handleOverride(payload: OverridePayload | SynthesisOverridePayload) {
    if (!ctx) return;
    setView("busy");
    try {
      await api.workflow.override(ctx.projectId, payload, DEV_TOKEN);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Override failed.");
      setView("error");
    }
  }

  async function handleSynthesisReject(feedback: string) {
    if (!ctx) return;
    setView("busy");
    try {
      await api.workflow.reject(ctx.projectId, feedback, DEV_TOKEN);
      setLogLines((l) => [...l, "↩  Rejected — Critic regenerating synthesis…"]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reject failed.");
      setView("error");
    }
  }

  async function handleTogglePaper(paper: Paper) {
    if (!ctx) return;
    try {
      const updated = await api.papers.setApproved(ctx.projectId, paper.id, !paper.approved, DEV_TOKEN);
      setPapers((ps) => ps.map((p) => (p.id === updated.id ? updated : p)));
    } catch { /* no-op */ }
  }

  const isBusy = view === "busy" || view === "creating";
  const approvedCount = papers.filter((p) => p.approved).length;

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--bg)" }}>

      {/* ── Top nav ─────────────────────────────────────────────────────── */}
      <header className="border-b border-[#1e2d45] px-6 py-3 flex items-center justify-between sticky top-0 z-10 backdrop-blur-sm"
        style={{ background: "rgba(10,15,30,0.85)" }}>
        <div className="flex items-center gap-3">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-blue-500/20 border border-blue-500/30">
            <svg className="h-3.5 w-3.5 text-blue-400" viewBox="0 0 16 16" fill="none">
              <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.5"/>
              <path d="M9 9l4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          </div>
          <span className="text-sm font-semibold tracking-tight text-slate-100">ResearchFlow AI</span>
          <span className="rounded-full bg-blue-500/10 border border-blue-500/20 px-2 py-0.5 text-[10px] font-medium text-blue-400 uppercase tracking-wider">
            {ctx ? phaseLabel(ctx.phase) : "Phase 1 + 2"}
          </span>
        </div>
        {ctx && (
          <div className="hidden sm:flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse-dot" />
            <span className="text-xs text-slate-500">Session active</span>
          </div>
        )}
      </header>

      {/* ── Main ────────────────────────────────────────────────────────── */}
      <main className="flex-1 px-4 py-10 sm:px-6">
        <div className="mx-auto max-w-2xl space-y-6">

          {/* Phase tracker */}
          {ctx && (
            <div className="animate-fade-in rounded-xl border border-[#1e2d45] bg-[#111827] px-5 py-4">
              <PhaseTracker current={ctx.phase} />
            </div>
          )}

          {/* ── IDLE: create form ───────────────────────────────────────── */}
          {view === "idle" && (
            <div className="animate-fade-in">
              {/* Hero */}
              <div className="mb-8 space-y-2">
                <h1 className="text-3xl font-bold tracking-tight text-slate-100">
                  Discovery
                </h1>
                <p className="text-sm text-slate-500 max-w-md">
                  Define your research topic. The Librarian agent will fetch and rank candidate papers from Semantic Scholar and arXiv.
                </p>
              </div>

              <form onSubmit={handleCreate}
                className="rounded-xl border border-[#1e2d45] bg-[#111827] p-6 space-y-5">
                <div className="space-y-1.5">
                  <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider" htmlFor="title">
                    Project title
                  </label>
                  <input
                    id="title"
                    type="text"
                    required
                    className="w-full rounded-lg border border-[#1e2d45] bg-[#0a0f1e] px-3.5 py-2.5 text-sm text-slate-200 placeholder-slate-600 transition-colors focus:border-blue-500/50 focus:outline-none focus:ring-1 focus:ring-blue-500/30"
                    placeholder="Survey of deep learning in medical imaging"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider" htmlFor="seed">
                    Seed query
                  </label>
                  <input
                    id="seed"
                    type="text"
                    required
                    className="w-full rounded-lg border border-[#1e2d45] bg-[#0a0f1e] px-3.5 py-2.5 text-sm text-slate-200 placeholder-slate-600 transition-colors focus:border-blue-500/50 focus:outline-none focus:ring-1 focus:ring-blue-500/30"
                    placeholder="convolutional neural networks histopathology classification"
                    value={seedQuery}
                    onChange={(e) => setSeedQuery(e.target.value)}
                  />
                  <p className="text-xs text-slate-600">The agent will expand this into multiple search queries automatically.</p>
                </div>
                <button
                  type="submit"
                  className="flex items-center gap-2 rounded-lg bg-blue-600 px-5 py-2.5 text-sm font-medium text-white transition-all hover:bg-blue-500 hover:shadow-[0_0_16px_rgba(59,130,246,0.3)] focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                >
                  <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                    <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                  Start Librarian
                </button>
              </form>
            </div>
          )}

          {/* ── CREATING ────────────────────────────────────────────────── */}
          {view === "creating" && (
            <div className="flex items-center gap-3 rounded-xl border border-[#1e2d45] bg-[#111827] px-5 py-4 text-sm text-slate-400">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-700 border-t-blue-500" />
              Creating project and connecting…
            </div>
          )}

          {/* ── RUNNING / BUSY ──────────────────────────────────────────── */}
          {(view === "running" || view === "busy") && (
            <div className="space-y-3 animate-fade-in">
              <div className="flex items-center gap-3 rounded-xl border border-[#1e2d45] bg-[#111827] px-5 py-3 text-sm text-slate-400">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-700 border-t-blue-500" />
                {view === "busy" ? "Waiting for workflow to advance…" : "Librarian is fetching papers…"}
              </div>
              <AgentLog lines={logLines} endRef={logEndRef} />
            </div>
          )}

          {/* ── SYNTHESIS (Phase 2) ─────────────────────────────────────── */}
          {view === "synthesis" && (
            <div className="space-y-4 animate-fade-in">
              <AgentLog lines={logLines} endRef={logEndRef} />
              <SynthesisReview
                matrix={matrix}
                summary={summary}
                papers={papers}
                loading={synthesisLoading}
                busy={false}
                onApprove={handleApprove}
                onReject={handleSynthesisReject}
                onOverride={handleOverride}
              />
            </div>
          )}

          {/* ── AWAITING (Phase 1) ──────────────────────────────────────── */}
          {view === "awaiting" && (
            <div className="space-y-4 animate-fade-in">
              <AgentLog lines={logLines} endRef={logEndRef} />

              {/* Paper list */}
              <div className="rounded-xl border border-[#1e2d45] bg-[#111827] overflow-hidden">
                <div className="flex items-center justify-between border-b border-[#1e2d45] px-5 py-4">
                  <div>
                    <h2 className="text-sm font-semibold text-slate-200">
                      Candidate papers
                    </h2>
                    <p className="mt-0.5 text-xs text-slate-500">
                      Click a title to open the source. Check papers to include in your approved pool.
                    </p>
                  </div>
                  {papers.length > 0 && (
                    <div className="shrink-0 rounded-full bg-blue-500/10 border border-blue-500/20 px-3 py-1 text-xs font-medium text-blue-400">
                      {approvedCount} / {papers.length} selected
                    </div>
                  )}
                </div>

                {papersLoading && (
                  <div className="flex items-center gap-3 p-5 text-sm text-slate-500">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-700 border-t-blue-500" />
                    Loading candidates…
                  </div>
                )}

                {!papersLoading && papers.length === 0 && (
                  <div className="flex flex-col items-center gap-2 py-10 text-center">
                    <span className="text-2xl">📭</span>
                    <p className="text-sm text-slate-500">No candidates found.</p>
                    <p className="text-xs text-slate-600">Try rejecting and regenerating with a broader query.</p>
                  </div>
                )}

                {!papersLoading && papers.length > 0 && (
                  <ul className="divide-y divide-[#1a2236]">
                    {papers.map((paper) => (
                      <li
                        key={paper.id}
                        className={`flex gap-4 px-5 py-4 transition-colors ${
                          paper.approved ? "bg-emerald-500/5" : "hover:bg-[#1a2236]"
                        }`}
                      >
                        <div className="pt-0.5">
                          <input
                            type="checkbox"
                            id={`p-${paper.id}`}
                            checked={paper.approved}
                            disabled={isBusy}
                            onChange={() => handleTogglePaper(paper)}
                            className="h-4 w-4 cursor-pointer rounded accent-emerald-500 disabled:cursor-not-allowed"
                          />
                        </div>
                        <label htmlFor={`p-${paper.id}`} className="flex-1 cursor-pointer space-y-1.5 min-w-0">
                          {/* Title */}
                          <p className="text-sm font-medium leading-snug text-slate-200">
                            <a
                              href={paperSourceUrl(paper)}
                              target="_blank"
                              rel="noopener noreferrer"
                              onClick={(e) => e.stopPropagation()}
                              className="hover:text-blue-400 hover:underline underline-offset-2 transition-colors"
                            >
                              {paper.title}
                              <svg className="ml-1 inline h-3 w-3 text-slate-600" viewBox="0 0 12 12" fill="none">
                                <path d="M3.5 2H2a1 1 0 00-1 1v7a1 1 0 001 1h7a1 1 0 001-1V8.5M7 1h4m0 0v4m0-4L5 7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
                              </svg>
                            </a>
                          </p>

                          {/* Meta row */}
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-xs text-slate-500">
                              {paper.authors.slice(0, 3).join(", ")}
                              {paper.authors.length > 3 && " et al."}
                              {paper.year ? ` · ${paper.year}` : ""}
                            </span>
                            <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${sourceBadgeClass(paper.source)}`}>
                              {sourceLabel(paper.source)}
                            </span>
                            <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                              {paper.citation_key}
                            </code>
                          </div>

                          {/* Abstract */}
                          {paper.abstract && (
                            <p className="line-clamp-2 text-xs leading-relaxed text-slate-500">
                              {paper.abstract}
                            </p>
                          )}
                        </label>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <ApprovalPanel
                summary={approvalSummary}
                busy={isBusy}
                onApprove={handleApprove}
                onReject={handleReject}
                onOverride={handleOverride}
              />
            </div>
          )}

          {/* ── DONE ────────────────────────────────────────────────────── */}
          {view === "done" && (() => {
            const synthesisDone = completedPhase === "synthesis";
            return (
            <div className="animate-fade-in space-y-4">
              <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/5 glow-green overflow-hidden">
                <div className="flex items-center gap-3 border-b border-emerald-500/20 px-5 py-4">
                  <div className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-500/20">
                    <svg className="h-3.5 w-3.5 text-emerald-400" viewBox="0 0 16 16" fill="none">
                      <path d="M3 8l4 4 6-7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  </div>
                  <p className="text-sm font-semibold text-emerald-300">
                    {synthesisDone ? "Phase 2 complete — synthesis approved" : "Phase 1 complete"}
                  </p>
                </div>
                <div className="px-5 py-4 space-y-4">
                  <p className="text-sm text-slate-400">
                    {synthesisDone ? (
                      <>
                        The literature synthesis is approved and locked. Phase 4 (Drafting)
                        will begin when the Scribe agent is enabled.
                      </>
                    ) : (
                      <>
                        <span className="font-semibold text-emerald-400">{approvedCount} paper{approvedCount !== 1 ? "s" : ""}</span>{" "}
                        approved and locked into your working pool. Phase 2 (Synthesis) will begin when ready.
                      </>
                    )}
                  </p>

                  {/* Phase 1: approved papers summary */}
                  {!synthesisDone && papers.filter((p) => p.approved).length > 0 && (
                    <ul className="space-y-1.5">
                      {papers.filter((p) => p.approved).map((p) => (
                        <li key={p.id} className="flex items-start gap-2 text-xs text-slate-400">
                          <span className="mt-0.5 text-emerald-500">✓</span>
                          <a
                            href={paperSourceUrl(p)}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="hover:text-blue-400 hover:underline underline-offset-2 transition-colors line-clamp-1"
                          >
                            {p.title}
                          </a>
                        </li>
                      ))}
                    </ul>
                  )}

                  {/* Phase 2: final synthesis read-only view */}
                  {synthesisDone && summary && (
                    <div className="rounded-lg border border-[#1e2d45] bg-[#0a0f1e] p-4">
                      <Markdown content={summary.content} />
                    </div>
                  )}

                  <AgentLog lines={logLines} endRef={logEndRef} />

                  <button
                    type="button"
                    onClick={() => {
                      setView("idle"); setCtx(null); setLogLines([]);
                      setPapers([]); setTitle(""); setSeedQuery("");
                      setMatrix(null); setSummary(null); setCompletedPhase(null);
                      wsRef.current?.close();
                    }}
                    className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-400 transition-all hover:bg-emerald-500/20"
                  >
                    Start new project
                  </button>
                </div>
              </div>
            </div>
            );
          })()}

          {/* ── ERROR ───────────────────────────────────────────────────── */}
          {view === "error" && (
            <div className="animate-fade-in rounded-xl border border-red-500/20 bg-red-500/5 overflow-hidden">
              <div className="flex items-center gap-3 border-b border-red-500/20 px-5 py-4">
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-red-500/20">
                  <svg className="h-3.5 w-3.5 text-red-400" viewBox="0 0 16 16" fill="none">
                    <path d="M8 5v4M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                    <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5"/>
                  </svg>
                </div>
                <p className="text-sm font-semibold text-red-300">Something went wrong</p>
              </div>
              <div className="px-5 py-4 space-y-4">
                {error && <p className="text-sm text-slate-400">{error}</p>}
                <button
                  type="button"
                  onClick={() => {
                    setView("idle"); setError(null); setCtx(null);
                    setLogLines([]); setPapers([]);
                    setMatrix(null); setSummary(null); setCompletedPhase(null);
                    wsRef.current?.close();
                  }}
                  className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-sm font-medium text-red-400 transition-all hover:bg-red-500/20"
                >
                  Try again
                </button>
              </div>
            </div>
          )}
        </div>
      </main>

      {/* ── Footer ──────────────────────────────────────────────────────── */}
      <footer className="border-t border-[#1e2d45] px-6 py-3 text-center text-xs text-slate-700">
        ResearchFlow AI · Discovery + Synthesis · Human-in-the-loop research automation
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AgentLog
// ---------------------------------------------------------------------------

function AgentLog({ lines, endRef }: { lines: string[]; endRef: React.RefObject<HTMLDivElement> }) {
  if (lines.length === 0) return null;
  return (
    <div className="rounded-xl border border-[#1e2d45] bg-[#0a0f1e] overflow-hidden">
      <div className="flex items-center gap-2 border-b border-[#1e2d45] px-4 py-2">
        <span className="h-2 w-2 rounded-full bg-red-500/60" />
        <span className="h-2 w-2 rounded-full bg-amber-500/60" />
        <span className="h-2 w-2 rounded-full bg-emerald-500/60" />
        <span className="ml-2 text-[10px] text-slate-600 font-mono uppercase tracking-wider">agent log</span>
      </div>
      <div className="max-h-44 overflow-y-auto p-4">
        {lines.map((line, i) => {
          const isStart = line.startsWith("▶");
          const isDone = line.startsWith("✓");
          const isError = line.startsWith("✗");
          return (
            <div
              key={i}
              className={`font-mono text-xs leading-relaxed ${
                isStart ? "text-blue-400"
                : isDone ? "text-emerald-400"
                : isError ? "text-red-400"
                : "text-slate-500"
              }`}
            >
              {line}
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
    </div>
  );
}
