"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApprovalPanel, type OverridePayload } from "@/components/workflow/ApprovalPanel";
import { DraftingTelemetryChips } from "@/components/workflow/DraftingTelemetryChips";
import { ExportPanel } from "@/components/workflow/ExportPanel";
import { Markdown } from "@/components/workflow/Markdown";
import { PhaseTracker } from "@/components/workflow/PhaseTracker";
import {
  SectionReview,
  type SectionOverridePayload,
} from "@/components/workflow/SectionReview";
import {
  SynthesisReadOnly,
  SynthesisReview,
  type SynthesisOverridePayload,
} from "@/components/workflow/SynthesisReview";
import { ApiError, api } from "@/lib/api";
import type { Artifact, Paper, Phase, SectionName, WorkflowState } from "@/lib/types";
import { cn, focusRing } from "@/lib/utils";
import { connectProjectEvents, type ManagedSocket, type ServerEvent } from "@/lib/ws";

// ---------------------------------------------------------------------------
// UI state machine
// ---------------------------------------------------------------------------

type View =
  | "idle"
  | "creating"
  | "running"
  | "awaiting"
  | "synthesis"
  | "drafting"
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
  // Source badges encode WHICH database a paper came from — kept multi-hue
  // because the colors carry information (not chrome). The previous palette
  // included blue for semantic_scholar; swapped to cyan-300 on slate (a
  // distinct neutral) so the trading-terminal palette has no pure blues
  // while keeping the four sources visually separable.
  // Source badges are categorical data encoding (distinguish providers at a
  // glance), so distinct hues are intentional here — this is the one place the
  // emerald-monochrome chrome rule yields to legibility. Chips are borderless
  // tint-only; no boxes. CORE keeps emerald since it is the brand hue.
  if (source === "arxiv") return "bg-orange-500/10 text-orange-400";
  if (source === "semantic_scholar") return "bg-cyan-500/10 text-cyan-300";
  if (source === "core") return "bg-primary/10 text-primary";
  if (source === "europe_pmc") return "bg-pink-500/10 text-pink-400";
  return "bg-surface-elevated text-muted";
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

/** Normalize a caught error into the {message, kind} shape the UI state holds.
 *
 * Recognizes ApiError (carries .kind directly), plain Error, and unknown
 * thrown values. M3-C: the kind lets the renderer pick a phase-conflict
 * banner for 409s vs. a generic toast for everything else. */
function describeError(err: unknown, fallback: string): { message: string; kind?: string } {
  if (err instanceof ApiError) {
    return { message: err.message || fallback, kind: err.kind };
  }
  if (err instanceof Error) {
    return { message: err.message || fallback };
  }
  return { message: fallback };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const DEV_TOKEN = process.env.NEXT_PUBLIC_DEV_TOKEN ?? "dev-token";

export default function HomePage() {
  const [title, setTitle] = useState("");
  const [seedQuery, setSeedQuery] = useState("");
  const [view, setView] = useState<View>("idle");
  // M3-C: capture the error category alongside the message so the renderer
  // can pick a recovery affordance per kind (conflict banner vs network
  // toast vs validation inline). The legacy code passed only a string,
  // collapsing every error class to the same generic "Something went wrong"
  // banner.
  const [error, setError] = useState<{ message: string; kind?: string } | null>(
    null,
  );
  const [ctx, setCtx] = useState<RunCtx | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const logEndRef = useRef<HTMLDivElement>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [papersLoading, setPapersLoading] = useState(false);
  const [approvalSummary, setApprovalSummary] = useState("");
  const [matrix, setMatrix] = useState<Artifact | null>(null);
  const [summary, setSummary] = useState<Artifact | null>(null);
  const [synthesisLoading, setSynthesisLoading] = useState(false);
  // Phase 4 — drafting state
  const [sectionArtifact, setSectionArtifact] = useState<Artifact | null>(null);
  const [currentSection, setCurrentSection] = useState<SectionName | null>(null);
  const [sectionLoading, setSectionLoading] = useState(false);
  const [manuscript, setManuscript] = useState<Artifact | null>(null);
  // Which phase the DONE screen should report as complete. Set when the
  // workflow finishes a phase — not inferred from ctx.phase, which is unreliable
  // (the backend reports phase="synthesis" even after synthesis is approved).
  const [completedPhase, setCompletedPhase] = useState<
    "discovery" | "synthesis" | "drafting" | null
  >(null);
  // Bump whenever a section_ready event lands so the telemetry chips refetch.
  const [telemetryRefresh, setTelemetryRefresh] = useState(0);
  // W2-C1: fulltext PDF ingest progress (between Phase-1 approve and the
  // Critic starting). null = idle, otherwise {done,total} for the busy chip.
  const [fulltextProgress, setFulltextProgress] = useState<
    { done: number; total: number } | null
  >(null);
  const wsRef = useRef<ManagedSocket | null>(null);
  // Wave-3/W2: dedupe duplicate approval.required events. Each section gate
  // can land twice (live emit + replay from the WS bus's last_event cache);
  // without this guard we double-fetch artifacts on every reconnect.
  const lastDraftingFetchRef = useRef<{ projectId: string; section: string } | null>(null);

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
        if (evt.phase === "drafting") {
          // Phase 4 — Scribe paused at a per-section gate. The event carries
          // the section name; fetch the latest section artifact (most recent
          // by created_at — the one the Scribe just produced).
          setCurrentSection(evt.section ?? null);
          setView("drafting");

          // Wave-3/W2: dedupe — if the same (project, section) just arrived,
          // we already have the artifact loaded; skip the redundant fetch +
          // telemetry refresh. WS replay + reconnect can each re-deliver the
          // last approval.required event.
          const sectionKey = evt.section ?? "";
          const last = lastDraftingFetchRef.current;
          if (last && last.projectId === projectId && last.section === sectionKey) {
            break;
          }
          lastDraftingFetchRef.current = { projectId, section: sectionKey };

          setSectionLoading(true);
          // Each section gate means a fresh phase_4.section_ready audit row
          // on the backend — bump the telemetry chips so they refetch /usage
          // and the counters track live work.
          setTelemetryRefresh((r) => r + 1);
          api.artifacts
            .list(projectId, "section", DEV_TOKEN)
            .then((sections) => {
              // The backend filter returns all section artifacts for the
              // project; pick the most recent one (the Scribe's latest draft).
              setSectionArtifact(latestArtifact(sections));
            })
            .catch(() => setError({ message: "Failed to load the drafted section." }))
            .finally(() => setSectionLoading(false));
        } else if (evt.phase === "synthesis") {
          // Phase 2 — load the Critic's matrix + summary artifacts, plus the
          // paper pool so the matrix can show titles (not just citation keys).
          setSynthesisLoading(true);
          setView("synthesis");
          // W2-C1: fulltext ingest is finished by now (the Critic just
          // synthesized). Clear the progress chip.
          setFulltextProgress(null);
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
            .catch(() => setError({ message: "Failed to load synthesis artifacts." }))
            .finally(() => setSynthesisLoading(false));
        } else if (evt.phase === "discovery") {
          // Phase 1 — load the candidate paper pool. Guard against a replayed
          // stale discovery event dragging the user back from a later phase.
          setPapersLoading(true);
          api.papers
            .list(projectId, DEV_TOKEN)
            .then((p) => setPapers(p))
            .catch(() => setError({ message: "Failed to load candidate papers." }))
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
        if (evt.phase === "done" && evt.state === "approved") {
          // Phase 4 complete — manuscript assembled. Fetch it for the done view.
          setCompletedPhase("drafting");
          setView("done");
          api.artifacts
            .list(projectId, "manuscript", DEV_TOKEN)
            .then((manuscripts) => setManuscript(latestArtifact(manuscripts)))
            .catch(() => {
              /* manuscript fetch is best-effort; done screen still shows logs */
            });
        } else if (evt.state === "approved" && evt.phase === "drafting") {
          // Phase 2 approve → Scribe is about to draft the first section.
          // Don't render the done screen here — the next approval.required
          // (phase=drafting, section=abstract) will switch us to drafting view.
          setLogLines((l) =>
            l.includes("✓  Synthesis approved — Scribe drafting…")
              ? l
              : [...l, "✓  Synthesis approved — Scribe drafting…"],
          );
          setView((v) => (v === "synthesis" ? "busy" : v));
        } else if (evt.state === "approved" && evt.phase === "synthesis") {
          // Phase 1 approve → Critic is about to synthesize. Show busy until
          // approval.required{phase:"synthesis"} arrives.
          setLogLines((l) =>
            l.includes("✓  Pool approved — Critic synthesizing…")
              ? l
              : [...l, "✓  Pool approved — Critic synthesizing…"],
          );
          setView((v) => (v === "awaiting" || v === "running" ? "busy" : v));
        } else if (evt.state === "awaiting_approval") {
          // handled by approval.required
        } else if (evt.state === "running" && evt.phase === "discovery") {
          setView((v) => (v === "idle" || v === "creating" ? "running" : v));
        } else if (evt.state === "error") {
          setError({ message: "Workflow encountered an error." });
          setView("error");
        }
        break;
      case "cost.cap_warn":
        setLogLines((l) => [
          ...l,
          `⚠  Spend $${evt.spend_usd.toFixed(2)} of $${evt.cap_usd.toFixed(2)} cap (${Math.round(evt.warn_pct * 100)}% threshold)`,
        ]);
        break;
      case "cost.cap_exceeded":
        setError({
          kind: "conflict",
          message: `Token cap reached: spent $${evt.spend_usd.toFixed(2)} of the $${evt.cap_usd.toFixed(2)} budget. Raise the project's cap to continue.`,
        });
        setView("error");
        break;
      case "fulltext_progress":
        // W2-C1: the Critic's fulltext fetcher reports per-paper completion.
        // We just track latest done/total; the busy-view chip reads this slot.
        // Clear automatically when the run advances past synthesis (handled
        // by approval.required for phase=synthesis, below).
        setFulltextProgress({ done: evt.done, total: evt.total });
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
    setSectionArtifact(null);
    setCurrentSection(null);
    setManuscript(null);
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
        onError: () => setError({ message: "WebSocket connection error.", kind: "network" }),
        onClose: (e) => {
          if (e.code !== 1000)
            setError({ message: `WebSocket closed (code ${e.code}).`, kind: "network" });
        },
      });
      await api.workflow.start(project.id, DEV_TOKEN);
      setView("running");
      setLogLines(["Project created. Librarian starting…"]);
    } catch (err) {
      setError(describeError(err, "Failed to start workflow."));
      setView("error");
    }
  }

  // Wave-3/C5: stable references so future React.memo'd children don't see
  // a new function each render. Each closes over `ctx` only — recompute when
  // the project changes.
  const handleApprove = useCallback(
    async (opts?: { force_unresolved?: boolean; override_reason?: string | null }) => {
      if (!ctx) return;
      setView("busy");
      try {
        await api.workflow.approve(
          ctx.projectId,
          {
            feedback: null,
            ...(opts?.force_unresolved ? { force_unresolved: true } : {}),
            ...(opts?.override_reason ? { override_reason: opts.override_reason } : {}),
          },
          DEV_TOKEN,
        );
      } catch (err) {
        // Re-throw the typed ApiError so the SectionReview can branch on a 409
        // unresolved_citations response (FR-1.5) without flipping to the error
        // view. Other callers still let it fall through to setError.
        if (err instanceof ApiError && err.code === "unresolved_citations") {
          setView("drafting");
          throw err;
        }
        setError(describeError(err, "Approve failed."));
        setView("error");
      }
    },
    [ctx],
  );

  const handleReject = useCallback(
    async (feedback: string) => {
      if (!ctx) return;
      setView("busy");
      try {
        await api.workflow.reject(ctx.projectId, feedback, DEV_TOKEN);
        setView("running");
        setLogLines((l) => [...l, "↩  Rejected — Librarian regenerating…"]);
      } catch (err) {
        setError(describeError(err, "Reject failed."));
        setView("error");
      }
    },
    [ctx],
  );

  const handleOverride = useCallback(
    async (payload: OverridePayload | SynthesisOverridePayload) => {
      if (!ctx) return;
      setView("busy");
      try {
        await api.workflow.override(ctx.projectId, payload, DEV_TOKEN);
      } catch (err) {
        setError(describeError(err, "Override failed."));
        setView("error");
      }
    },
    [ctx],
  );

  const handleSynthesisReject = useCallback(
    async (feedback: string) => {
      if (!ctx) return;
      setView("busy");
      try {
        await api.workflow.reject(ctx.projectId, feedback, DEV_TOKEN);
        setLogLines((l) => [...l, "↩  Rejected — Critic regenerating synthesis…"]);
      } catch (err) {
        setError(describeError(err, "Reject failed."));
        setView("error");
      }
    },
    [ctx],
  );

  async function handleTogglePaper(paper: Paper) {
    if (!ctx) return;
    try {
      const updated = await api.papers.setApproved(ctx.projectId, paper.id, !paper.approved, DEV_TOKEN);
      setPapers((ps) => ps.map((p) => (p.id === updated.id ? updated : p)));
    } catch { /* no-op */ }
  }

  const isBusy = view === "busy" || view === "creating";
  const approvedCount = papers.filter((p) => p.approved).length;

  const phaseTitle = ctx ? phaseLabel(ctx.phase) : "Discovery";

  return (
    // App shell — top-level CSS Grid: fixed left nav rail + full-bleed content
    // column. No nested sidebar-within-sidebar; no centered max-w cap. The rail
    // is its own grid track so the main area sprawls across the rest of the
    // viewport (the matrix + manuscript get real room to breathe).
    <div className="grid min-h-screen grid-cols-[15rem_1fr] bg-background text-foreground">

      {/* ── Left nav rail ───────────────────────────────────────────────── */}
      <aside className="sticky top-0 flex h-screen flex-col gap-8 px-6 py-7">
        {/* Brand */}
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/15 text-primary">
            <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none">
              <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.5" />
              <path d="M9 9l4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </span>
          <span className="font-display text-base font-bold tracking-tight">ResearchFlow</span>
        </div>

        {/* Phase tracker — vertical in the rail */}
        <nav className="flex-1">
          <p className="mb-4 font-mono text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
            Pipeline
          </p>
          <PhaseTracker current={ctx?.phase ?? "discovery"} />
        </nav>

        {/* Session status */}
        <div className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
          {ctx ? (
            <>
              <span className="h-1.5 w-1.5 rounded-full bg-primary animate-pulse-dot" />
              <span>session active</span>
            </>
          ) : (
            <>
              <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
              <span>idle</span>
            </>
          )}
        </div>
      </aside>

      {/* ── Content column ──────────────────────────────────────────────── */}
      <div className="flex min-w-0 flex-col">

        {/* Slim top bar — current phase + live status. No border box; the
            backdrop blur + faint elevation reads as a bar without a hard line. */}
        <header className="sticky top-0 z-10 flex items-center justify-between px-10 py-5 backdrop-blur-sm">
          <div className="flex items-baseline gap-3">
            <h1 className="font-display text-2xl font-extrabold tracking-tight">{phaseTitle}</h1>
            <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-primary/70">
              {ctx ? "human-in-the-loop" : "ready"}
            </span>
          </div>
        </header>

        {/* Main — full-bleed, generous padding, no max-w cap. Whitespace and
            type scale carry the hierarchy; data-heavy views fill the width. */}
        <main className="min-w-0 flex-1 px-10 pb-16">

          {/* ── IDLE: create form ─────────────────────────────────────────── */}
          {view === "idle" && (
            <div className="max-w-xl animate-fade-in">
              <p className="mb-10 text-sm leading-relaxed text-muted">
                Define your research topic. The Librarian agent fetches and ranks candidate
                papers from Semantic Scholar and arXiv.
              </p>

              <form onSubmit={handleCreate} className="space-y-7">
                <div className="space-y-2">
                  <label className="block font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground" htmlFor="title">
                    Project title
                  </label>
                  <input
                    id="title"
                    type="text"
                    required
                    className="w-full bg-transparent pb-2 text-lg text-foreground placeholder-muted-foreground/50 outline-none transition-all duration-200 [border-bottom:1px_solid_var(--color-border)] focus:[border-bottom-color:var(--color-primary)] focus:[box-shadow:0_1px_0_0_var(--color-primary)]"
                    placeholder="Survey of deep learning in medical imaging"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <label className="block font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground" htmlFor="seed">
                    Seed query
                  </label>
                  <input
                    id="seed"
                    type="text"
                    required
                    className="w-full bg-transparent pb-2 text-lg text-foreground placeholder-muted-foreground/50 outline-none transition-all duration-200 [border-bottom:1px_solid_var(--color-border)] focus:[border-bottom-color:var(--color-primary)] focus:[box-shadow:0_1px_0_0_var(--color-primary)]"
                    placeholder="convolutional neural networks histopathology classification"
                    value={seedQuery}
                    onChange={(e) => setSeedQuery(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    The agent expands this into multiple search queries automatically.
                  </p>
                </div>
                <button
                  type="submit"
                  className="inline-flex items-center gap-2 rounded-md bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground transition-all duration-200 hover:bg-primary-hover hover:shadow-[0_0_20px_oklch(72%_0.20_155_/_0.35)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 active:scale-[0.98]"
                >
                  <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                    <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  </svg>
                  Start Librarian
                </button>
              </form>
            </div>
          )}

          {/* ── CREATING ──────────────────────────────────────────────────── */}
          {view === "creating" && (
            <div className="flex items-center gap-3 text-sm text-muted animate-fade-in">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-primary" />
              Creating project and connecting…
            </div>
          )}

          {/* ── RUNNING / BUSY ────────────────────────────────────────────── */}
          {(view === "running" || view === "busy") && (
            <div className="max-w-3xl space-y-6 animate-fade-in">
              <div className="flex items-center gap-3 text-sm text-muted">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-border border-t-primary" />
                {view === "busy" ? "Waiting for the workflow to advance…" : "Librarian is fetching papers…"}
              </div>

              {/* W2-C1: live fulltext-ingest progress while the Critic prepares
                  RAG context (used to be a silent ~120s gap). */}
              {fulltextProgress && fulltextProgress.total > 0 && (
                <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.12em] text-muted">
                  <span>fulltext indexed</span>
                  <span className="text-primary">
                    {fulltextProgress.done}/{fulltextProgress.total} papers
                  </span>
                  <div className="h-1 flex-1 max-w-xs overflow-hidden rounded-full bg-surface-elevated">
                    <div
                      className="h-full rounded-full bg-primary transition-all duration-300"
                      style={{
                        width: `${Math.min(100, (fulltextProgress.done / fulltextProgress.total) * 100)}%`,
                      }}
                    />
                  </div>
                </div>
              )}

              <AgentLog lines={logLines} endRef={logEndRef} />
            </div>
          )}

          {/* ── SYNTHESIS (Phase 2) — full-bleed for the matrix ───────────── */}
          {view === "synthesis" && (
            <div className="space-y-6 animate-fade-in">
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

          {/* ── DRAFTING (Phase 4) — full-bleed for the manuscript ────────── */}
          {view === "drafting" && (
            <div className="space-y-6 animate-fade-in">
              <AgentLog lines={logLines} endRef={logEndRef} />
              {ctx && (
                <DraftingTelemetryChips
                  projectId={ctx.projectId}
                  token={DEV_TOKEN}
                  refreshKey={telemetryRefresh}
                />
              )}
              <SectionReview
                section={sectionArtifact}
                currentSection={currentSection}
                projectId={ctx?.projectId ?? ""}
                loading={sectionLoading}
                busy={false}
                onApprove={handleApprove}
                onReject={handleSynthesisReject}
                onOverride={handleOverride}
              />
            </div>
          )}

          {/* ── AWAITING (Phase 1) — borderless paper list ────────────────── */}
          {view === "awaiting" && (
            <div className="max-w-4xl space-y-8 animate-fade-in">
              <AgentLog lines={logLines} endRef={logEndRef} />

              {/* Section header — whitespace + type, no box */}
              <div className="flex items-end justify-between">
                <div className="space-y-1">
                  <h2 className="font-display text-lg font-bold">Candidate papers</h2>
                  <p className="text-xs text-muted-foreground">
                    Open a title to view the source. Select papers for your approved pool.
                  </p>
                </div>
                {papers.length > 0 && (
                  <span className="shrink-0 font-mono text-xs text-primary">
                    {approvedCount} / {papers.length} selected
                  </span>
                )}
              </div>

              {papersLoading && (
                <div className="space-y-3">
                  {[0, 1, 2, 3].map((i) => (
                    <div key={i} className="flex gap-4 py-2">
                      <div className="skeleton h-4 w-4 shrink-0 rounded" />
                      <div className="flex-1 space-y-2">
                        <div className="skeleton h-4 w-3/4" />
                        <div className="skeleton h-3 w-1/3" />
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {!papersLoading && papers.length === 0 && (
                <div className="py-12 text-center">
                  <p className="text-sm text-muted">No candidates found.</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Try rejecting and regenerating with a broader query.
                  </p>
                </div>
              )}

              {!papersLoading && papers.length > 0 && (
                // Rows separated by hairline dividers, not boxed cards.
                <ul className="divide-y divide-border">
                  {papers.map((paper) => (
                    <li
                      key={paper.id}
                      className={cn(
                        "group/paper flex gap-4 rounded-md px-2 py-4 transition-colors duration-150 ease-in-out",
                        paper.approved ? "bg-primary/[0.05]" : "hover:bg-primary/[0.04]",
                      )}
                    >
                      <div className="pt-0.5">
                        <input
                          type="checkbox"
                          id={`p-${paper.id}`}
                          checked={paper.approved}
                          disabled={isBusy}
                          onChange={() => handleTogglePaper(paper)}
                          className={cn(
                            "h-4 w-4 cursor-pointer rounded accent-primary disabled:cursor-not-allowed",
                            focusRing,
                          )}
                        />
                      </div>
                      <label htmlFor={`p-${paper.id}`} className="min-w-0 flex-1 cursor-pointer space-y-1.5">
                        <p className="text-sm font-medium leading-snug text-foreground">
                          <a
                            href={paperSourceUrl(paper)}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            className="underline-offset-2 transition-colors duration-200 hover:text-primary hover:underline"
                          >
                            {paper.title}
                            <svg className="ml-1 inline h-3 w-3 text-muted-foreground" viewBox="0 0 12 12" fill="none">
                              <path d="M3.5 2H2a1 1 0 00-1 1v7a1 1 0 001 1h7a1 1 0 001-1V8.5M7 1h4m0 0v4m0-4L5 7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                          </a>
                        </p>

                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-xs text-muted">
                            {paper.authors.slice(0, 3).join(", ")}
                            {paper.authors.length > 3 && " et al."}
                            {paper.year ? ` · ${paper.year}` : ""}
                          </span>
                          <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", sourceBadgeClass(paper.source))}>
                            {sourceLabel(paper.source)}
                          </span>
                          <code className="font-mono text-[10px] text-muted-foreground">
                            {paper.citation_key}
                          </code>
                        </div>

                        {paper.abstract && (
                          <p className="line-clamp-2 text-xs leading-relaxed text-muted">
                            {paper.abstract}
                          </p>
                        )}
                      </label>
                    </li>
                  ))}
                </ul>
              )}

              <ApprovalPanel
                summary={approvalSummary}
                busy={isBusy}
                onApprove={handleApprove}
                onReject={handleReject}
                onOverride={handleOverride}
              />
            </div>
          )}

          {/* ── DONE ──────────────────────────────────────────────────────── */}
          {view === "done" && (() => {
            const draftingDone = completedPhase === "drafting";
            const synthesisDone = completedPhase === "synthesis";
            const headline = draftingDone
              ? "Manuscript complete — all sections approved"
              : synthesisDone
                ? "Synthesis approved"
                : "Pool approved";
            return (
              <div className="space-y-8 animate-fade-in">
                {/* Completion banner — emerald accent via a left rule + glow,
                    not a boxed card. */}
                <div className="flex items-center gap-3 border-l-2 border-primary pl-4">
                  <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/15 text-primary">
                    <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                      <path d="M3 8l4 4 6-7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </span>
                  <h2 className="font-display text-lg font-bold text-foreground">{headline}</h2>
                </div>

                <p className="max-w-2xl text-sm leading-relaxed text-muted">
                  {draftingDone ? (
                    "Every section has been reviewed and approved. The full manuscript is rendered below — download it as Markdown."
                  ) : synthesisDone ? (
                    "The literature synthesis is approved and locked. Phase 4 (Drafting) begins when the Scribe agent is enabled."
                  ) : (
                    <>
                      <span className="font-semibold text-primary">{approvedCount} paper{approvedCount !== 1 ? "s" : ""}</span>{" "}
                      approved and locked into your working pool. Phase 2 (Synthesis) begins when ready.
                    </>
                  )}
                </p>

                {/* Phase 1: approved papers summary */}
                {!synthesisDone && !draftingDone && papers.filter((p) => p.approved).length > 0 && (
                  <ul className="max-w-2xl space-y-2">
                    {papers.filter((p) => p.approved).map((p) => (
                      <li key={p.id} className="flex items-start gap-2 text-xs text-muted">
                        <span className="mt-0.5 text-primary">✓</span>
                        <a
                          href={paperSourceUrl(p)}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="line-clamp-1 underline-offset-2 transition-colors duration-200 hover:text-primary hover:underline"
                        >
                          {p.title}
                        </a>
                      </li>
                    ))}
                  </ul>
                )}

                {/* Phase 2: final synthesis read-only — full-bleed. */}
                {synthesisDone && (summary || matrix) && (
                  <SynthesisReadOnly matrix={matrix} summary={summary} papers={papers} />
                )}

                {/* Phase 4: assembled manuscript + Export Pack picker. */}
                {draftingDone && manuscript && ctx && (
                  <div className="space-y-6">
                    <DraftingTelemetryChips
                      projectId={ctx.projectId}
                      token={DEV_TOKEN}
                      refreshKey={telemetryRefresh}
                    />

                    <ExportPanel projectId={ctx.projectId} token={DEV_TOKEN} />

                    <div className="space-y-3">
                      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                        Final manuscript preview
                      </p>
                      {/* max-w-[68ch] caps line length at the optimal reading
                          measure so prose never stretches across an ultrawide
                          monitor. Centered, generous vertical scroll region. */}
                      <div className="mx-auto max-h-[72vh] max-w-[68ch] overflow-y-auto pr-2">
                        <Markdown content={manuscript.content} variant="prose" />
                      </div>
                    </div>
                  </div>
                )}

                <AgentLog lines={logLines} endRef={logEndRef} />

                <button
                  type="button"
                  onClick={() => {
                    setView("idle"); setCtx(null); setLogLines([]);
                    setPapers([]); setTitle(""); setSeedQuery("");
                    setMatrix(null); setSummary(null); setCompletedPhase(null);
                    setSectionArtifact(null); setCurrentSection(null); setManuscript(null);
                    // Wave-3/W2: reset the dedupe marker so a fresh project
                    // never inherits the previous project's section key.
                    lastDraftingFetchRef.current = null;
                    setFulltextProgress(null);
                    wsRef.current?.close();
                  }}
                  className="inline-flex items-center rounded-md px-4 py-2 text-sm font-semibold text-primary transition-all duration-200 hover:bg-primary/10"
                >
                  Start new project
                </button>
              </div>
            );
          })()}

          {/* ── ERROR (phase conflict) ────────────────────────────────────── */}
          {view === "error" && error?.kind === "conflict" && (
            <div className="max-w-2xl space-y-4 border-l-2 border-warning pl-5 animate-fade-in">
              <div className="flex items-center gap-2.5">
                <svg className="h-4 w-4 text-warning" viewBox="0 0 16 16" fill="none">
                  <path d="M8 3v6M8 12v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
                </svg>
                <h2 className="font-display text-base font-bold text-warning">
                  Workflow phase already advanced
                </h2>
              </div>
              <p className="text-sm text-foreground">{error.message}</p>
              <p className="text-xs text-muted-foreground">
                The workflow moved past this gate while your tab was open. Reload to fetch
                the latest project state.
              </p>
              <button
                type="button"
                onClick={() => {
                  if (typeof window !== "undefined") window.location.reload();
                }}
                className="inline-flex items-center rounded-md px-4 py-2 text-sm font-semibold text-warning transition-all duration-200 hover:bg-warning/10"
              >
                Reload
              </button>
            </div>
          )}
          {view === "error" && error?.kind !== "conflict" && (
            <div className="max-w-2xl space-y-4 border-l-2 border-destructive pl-5 animate-fade-in">
              <div className="flex items-center gap-2.5">
                <svg className="h-4 w-4 text-destructive" viewBox="0 0 16 16" fill="none">
                  <path d="M8 5v4M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
                </svg>
                <h2 className="font-display text-base font-bold text-destructive">Something went wrong</h2>
              </div>
              {error && <p className="text-sm text-muted">{error.message}</p>}
              <button
                type="button"
                onClick={() => {
                  setView("idle"); setError(null); setCtx(null);
                  setLogLines([]); setPapers([]);
                  setMatrix(null); setSummary(null); setCompletedPhase(null);
                  wsRef.current?.close();
                }}
                className="inline-flex items-center rounded-md px-4 py-2 text-sm font-semibold text-destructive transition-all duration-200 hover:bg-destructive/10"
              >
                Try again
              </button>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AgentLog
// ---------------------------------------------------------------------------

function AgentLog({ lines, endRef }: { lines: string[]; endRef: React.RefObject<HTMLDivElement> }) {
  if (lines.length === 0) return null;
  // Borderless terminal: a faint elevated surface + mono type reads as a
  // console without drawing a hard box around it.
  return (
    <div className="overflow-hidden rounded-lg bg-surface-elevated/60">
      <div className="flex items-center gap-1.5 px-4 py-2.5">
        <span className="h-2 w-2 rounded-full bg-destructive/50" />
        <span className="h-2 w-2 rounded-full bg-warning/50" />
        <span className="h-2 w-2 rounded-full bg-primary/50" />
        <span className="ml-2 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          agent log
        </span>
      </div>
      <div className="max-h-44 overflow-y-auto px-4 pb-4">
        {lines.map((line, i) => {
          const isStart = line.startsWith("▶");
          const isDone = line.startsWith("✓");
          const isError = line.startsWith("✗");
          return (
            <div
              key={i}
              className={cn(
                "font-mono text-xs leading-relaxed",
                isStart ? "text-primary/70"
                  : isDone ? "text-primary"
                    : isError ? "text-destructive"
                      : "text-muted",
              )}
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
