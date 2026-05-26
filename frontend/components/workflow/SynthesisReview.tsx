"use client";

import { useMemo, useState } from "react";

import { diffLines, diffStats, type DiffOp } from "@/components/workflow/diffLines";
import { Markdown } from "@/components/workflow/Markdown";
import type { Artifact, Paper } from "@/lib/types";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Matrix shape — mirrors backend app/agents/critic.py MatrixModel / PaperExtraction
// ---------------------------------------------------------------------------

interface PaperRow {
  citation_key: string;
  problem: string;
  method: string;
  dataset: string;
  key_findings: string;
  limitations: string;
  extraction_failed: boolean;
  error: string | null;
}

interface MatrixModel {
  rows: PaperRow[];
}

const MATRIX_COLUMNS: { key: keyof PaperRow; label: string }[] = [
  { key: "problem", label: "Problem" },
  { key: "method", label: "Method" },
  { key: "dataset", label: "Dataset" },
  { key: "key_findings", label: "Key findings" },
  { key: "limitations", label: "Limitations" },
];

function parseMatrix(content: string): MatrixModel | null {
  try {
    const parsed = JSON.parse(content) as unknown;
    if (
      parsed &&
      typeof parsed === "object" &&
      "rows" in parsed &&
      Array.isArray((parsed as MatrixModel).rows)
    ) {
      return parsed as MatrixModel;
    }
    return null;
  } catch {
    return null;
  }
}

// Provider error blobs from any upstream LLM/HTTP call arrive as opaque
// strings. Categorize the known shapes so the UI can render a short,
// actionable message AND offer the right recovery affordance
// (retry vs. wait vs. reconfigure). Copy is intentionally provider-agnostic
// so swapping providers (Gemini → Anthropic → OpenAI → …) needs no UI
// change (coderabbit PR #5 finding G3). The status-code regexes still
// detect the underlying failure category regardless of provider.
type ErrorKind = "quota" | "overload" | "auth" | "schema" | "network" | "unknown";

interface ClassifiedError {
  kind: ErrorKind;
  /** One-sentence human-readable headline. */
  message: string;
  /** Short action prompt — what the user should do next. */
  hint: string;
  /** True if a retry might succeed without user intervention. */
  retryable: boolean;
}

function classifyError(raw: string | null): ClassifiedError {
  if (!raw) {
    return {
      kind: "unknown",
      message: "Extraction failed.",
      hint: "Reject & regenerate to retry.",
      retryable: true,
    };
  }
  // 429 / RESOURCE_EXHAUSTED — hard quota exhaustion.
  if (/RESOURCE_EXHAUSTED/.test(raw) || /\b429\b/.test(raw)) {
    return {
      kind: "quota",
      message: "LLM API quota exhausted (HTTP 429).",
      hint: "The provider's request/token budget is spent. Wait for the quota window to reset, or upgrade the API plan.",
      retryable: false,
    };
  }
  // 503 / UNAVAILABLE / "high demand" — transient upstream overload.
  if (/UNAVAILABLE/.test(raw) || /\b503\b/.test(raw) || /experiencing high demand/i.test(raw)) {
    return {
      kind: "overload",
      message: "The LLM provider is temporarily overloaded (HTTP 503).",
      hint: "This is transient. Wait 30–60 seconds, then click Reject & regenerate.",
      retryable: true,
    };
  }
  // 401 / 403 — bad or missing credentials.
  if (
    /\b401\b/.test(raw) ||
    /\b403\b/.test(raw) ||
    /UNAUTHENTICATED/.test(raw) ||
    /PERMISSION_DENIED/.test(raw)
  ) {
    return {
      kind: "auth",
      message: "The LLM API key is invalid or lacks the required permissions.",
      hint: "Check the backend's API key configuration and the provider's permission docs.",
      retryable: false,
    };
  }
  // 400 + schema/validation errors — usually means the abstract was empty or unsafe.
  if (/\b400\b/.test(raw) || /INVALID_ARGUMENT/.test(raw) || /SAFETY/i.test(raw)) {
    return {
      kind: "schema",
      message: "The paper's abstract could not be processed by the model.",
      hint: "The abstract may be empty, too long, or flagged by the provider's safety filters.",
      retryable: false,
    };
  }
  // Connection / DNS / timeout.
  if (/ECONNRESET|ETIMEDOUT|getaddrinfo|connection|timeout/i.test(raw)) {
    return {
      kind: "network",
      message: "Could not reach the LLM API.",
      hint: "Check the backend's network access and DNS, then regenerate.",
      retryable: true,
    };
  }
  // Try to lift the `'message': '...'` field out of a stringified error.
  const msg = /['"]message['"]\s*:\s*['"]([^'"]+)['"]/.exec(raw);
  return {
    kind: "unknown",
    message: msg && msg[1] ? msg[1] : raw.length > 160 ? `${raw.slice(0, 160)}…` : raw,
    hint: "Reject & regenerate to retry.",
    retryable: true,
  };
}

/** Legacy helper kept for the existing call site; new code uses classifyError. */
function humanizeError(raw: string | null): string {
  return classifyError(raw).message;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface SynthesisOverridePayload {
  artifact_kind: "summary";
  label: string;
  content: string;
  mime_type: string;
}

interface SynthesisReviewProps {
  matrix: Artifact | null;
  summary: Artifact | null;
  /** Approved papers — used to resolve a citation_key to a human-readable title. */
  papers: Paper[];
  loading: boolean;
  busy: boolean;
  onApprove: () => void;
  onReject: (feedback: string) => void;
  onOverride: (payload: SynthesisOverridePayload) => void;
}

type Tab = "narrative" | "matrix";
type Action = "idle" | "reject" | "override";
type EditView = "edit" | "diff" | "preview";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SynthesisReview({
  matrix,
  summary,
  papers,
  loading,
  busy,
  onApprove,
  onReject,
  onOverride,
}: SynthesisReviewProps) {
  const [tab, setTab] = useState<Tab>("narrative");
  const [action, setAction] = useState<Action>("idle");
  const [feedback, setFeedback] = useState("");
  const [editContent, setEditContent] = useState("");
  // Override mode shows three sub-views: raw editor, diff vs original, preview.
  const [editView, setEditView] = useState<EditView>("edit");

  const parsedMatrix = useMemo(
    () => (matrix ? parseMatrix(matrix.content) : null),
    [matrix],
  );

  // Resolve a citation_key → the paper's title + authors, so the matrix shows
  // a human-readable identity instead of an opaque key like "ahmadi2023".
  const paperByKey = useMemo(() => {
    const map = new Map<string, Paper>();
    for (const p of papers) map.set(p.citation_key, p);
    return map;
  }, [papers]);

  // Diff of the original Critic output vs. the user's edits. Re-computed only
  // when either side changes — keeps the LCS table cheap.
  const diffOps = useMemo(
    () => (action === "override" ? diffLines(summary?.content ?? "", editContent) : []),
    [action, summary?.content, editContent],
  );
  const stats = useMemo(() => diffStats(diffOps), [diffOps]);

  // The Critic's summary artifact is "## Comparison Matrix <table> ## Synthesis
  // <prose>". The matrix tab already renders the grid from JSON, so the
  // narrative tab shows only the prose after the "## Synthesis" heading.
  const summaryContent = summary?.content ?? "";
  const synthesisIdx = summaryContent.search(/##\s*Synthesis\b/i);
  const narrative =
    synthesisIdx >= 0 ? summaryContent.slice(synthesisIdx) : summaryContent;
  const paperCount = parsedMatrix?.rows.length ?? 0;
  const failedCount = parsedMatrix?.rows.filter((r) => r.extraction_failed).length ?? 0;
  const allFailed = paperCount > 0 && failedCount === paperCount;
  // The Critic writes "Narrative generation failed: …" when the synthesis LLM call errors.
  const narrativeFailed = /##\s*Synthesis\s*\n+Narrative generation failed/i.test(narrative);

  function startEditing() {
    // Edit the full summary artifact (matrix table + narrative) so the override
    // replaces the complete document, not just the prose section.
    setEditContent(summaryContent);
    setEditView("edit");
    setAction("override");
  }

  function handleRejectSubmit() {
    if (!feedback.trim()) return;
    onReject(feedback.trim());
  }

  function handleOverrideSubmit() {
    if (!editContent.trim()) return;
    onOverride({
      artifact_kind: "summary",
      label: summary?.label ?? "literature-summary",
      content: editContent.trim(),
      mime_type: "text/markdown",
    });
  }

  // ── Loading ───────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-[#1e2d45] bg-[#111827] px-5 py-4 text-sm text-slate-500">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-700 border-t-violet-500" />
        Loading the literature synthesis…
      </div>
    );
  }

  // ── Empty (artifacts not yet available) ───────────────────────────────
  if (!matrix && !summary) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-[#1e2d45] bg-[#111827] py-10 text-center">
        <span className="text-2xl">🧪</span>
        <p className="text-sm text-slate-500">No synthesis artifacts yet.</p>
        <p className="text-xs text-slate-600">The Critic may still be working.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4 animate-fade-in">
      {/* ── Synthesis card ─────────────────────────────────────────────── */}
      <div className="overflow-hidden rounded-xl border border-violet-500/20 bg-[#111827]">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-[#1e2d45] px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <span className="flex h-5 w-5 items-center justify-center rounded bg-violet-500/20 text-[10px] text-violet-300">
                02
              </span>
              Literature synthesis
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              The Critic compared {paperCount} paper{paperCount !== 1 ? "s" : ""} and wrote a narrative review.
            </p>
          </div>
          {failedCount > 0 && (
            <span
              className={cn(
                "shrink-0 rounded-full border px-3 py-1 text-xs font-medium",
                allFailed
                  ? "border-red-500/30 bg-red-500/10 text-red-400"
                  : "border-amber-500/20 bg-amber-500/10 text-amber-400",
              )}
            >
              {failedCount}/{paperCount} extraction{failedCount !== 1 ? "s" : ""} failed
            </span>
          )}
        </div>

        {/* Failure banner — surfaces the root cause without the JSON wall. */}
        {(allFailed || narrativeFailed) && (
          <div className="border-b border-red-500/20 bg-red-500/5 px-5 py-3">
            <div className="flex items-start gap-2.5">
              <svg
                className="mt-0.5 h-4 w-4 shrink-0 text-red-400"
                viewBox="0 0 16 16"
                fill="none"
              >
                <path d="M8 5v4M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
              </svg>
              <div className="space-y-0.5">
                <p className="text-xs font-semibold text-red-300">
                  The Critic could not complete the synthesis
                </p>
                <p className="text-xs text-slate-400">
                  {humanizeError(
                    parsedMatrix?.rows.find((r) => r.extraction_failed)?.error ?? narrative,
                  )}{" "}
                  Use <span className="text-slate-300">Reject &amp; regenerate</span> once the
                  provider quota recovers.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-[#1e2d45] px-3 pt-3">
          <TabButton active={tab === "narrative"} onClick={() => setTab("narrative")}>
            Narrative
          </TabButton>
          <TabButton active={tab === "matrix"} onClick={() => setTab("matrix")}>
            Comparison matrix
          </TabButton>
        </div>

        {/* Tab body */}
        <div className="px-5 py-5">
          {tab === "narrative" &&
            (!narrative ? (
              <p className="text-sm text-slate-500">No narrative was produced.</p>
            ) : narrativeFailed ? (
              <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-4">
                <p className="text-sm font-medium text-red-300">Narrative generation failed</p>
                <p className="mt-1 text-xs leading-relaxed text-slate-400">
                  {humanizeError(narrative)}
                </p>
              </div>
            ) : (
              <Markdown content={narrative} />
            ))}

          {tab === "matrix" &&
            (parsedMatrix && parsedMatrix.rows.length > 0 ? (
              <MatrixTable rows={parsedMatrix.rows} paperByKey={paperByKey} />
            ) : (
              <p className="text-sm text-slate-500">
                The comparison matrix could not be parsed.
              </p>
            ))}
        </div>
      </div>

      {/* ── Approval panel ─────────────────────────────────────────────── */}
      <div className="overflow-hidden rounded-xl border border-amber-500/20 bg-amber-500/5 glow-amber">
        <div className="flex items-center gap-3 border-b border-amber-500/20 px-5 py-4">
          <span className="flex h-2 w-2 rounded-full bg-amber-400 animate-pulse-dot" />
          <p className="text-sm font-semibold text-amber-300">Review the synthesis</p>
        </div>

        <div className="space-y-4 px-5 py-4">
          {action === "idle" && (
            <>
              <p className="text-sm text-slate-400">
                Approve to advance to drafting, reject with feedback to regenerate, or
                edit the narrative directly.
              </p>
              <div className="flex flex-wrap gap-3">
                <button
                  type="button"
                  onClick={onApprove}
                  disabled={busy}
                  className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-400 transition-all hover:border-emerald-500/50 hover:bg-emerald-500/20 hover:shadow-[0_0_12px_rgba(16,185,129,0.2)] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy ? (
                    <>
                      <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-emerald-400/30 border-t-emerald-400" />
                      Working…
                    </>
                  ) : (
                    <>
                      <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                        <path
                          d="M3 8l4 4 6-7"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                      Approve &amp; draft
                    </>
                  )}
                </button>

                <button
                  type="button"
                  onClick={() => setAction("reject")}
                  disabled={busy}
                  className="flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm font-medium text-amber-400 transition-all hover:border-amber-500/50 hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                    <path
                      d="M8 3v5M8 10v.5"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                    />
                  </svg>
                  Reject &amp; regenerate
                </button>

                <button
                  type="button"
                  onClick={startEditing}
                  disabled={busy || !summary}
                  className="flex items-center gap-2 rounded-lg border border-slate-600/50 bg-slate-700/50 px-4 py-2 text-sm font-medium text-slate-300 transition-all hover:border-slate-500 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                    <path
                      d="M11 2l3 3-8 8H3v-3l8-8z"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                  Edit narrative
                </button>
              </div>
            </>
          )}

          {action === "reject" && (
            <div className="space-y-3 animate-fade-in">
              <label className="block text-xs font-medium uppercase tracking-wider text-slate-400">
                Feedback for the Critic
              </label>
              <textarea
                className="w-full rounded-lg border border-slate-700 bg-slate-900/80 p-3 text-sm text-slate-200 placeholder-slate-600 transition-colors focus:border-amber-500/50 focus:outline-none focus:ring-1 focus:ring-amber-500/30"
                rows={3}
                placeholder="e.g. Group the synthesis by application domain, not by method…"
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                autoFocus
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={handleRejectSubmit}
                  disabled={busy || !feedback.trim()}
                  className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm font-medium text-amber-400 transition-all hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy ? "Working…" : "Submit & regenerate"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAction("idle");
                    setFeedback("");
                  }}
                  disabled={busy}
                  className="rounded-lg border border-slate-700 px-4 py-2 text-sm font-medium text-slate-400 transition-all hover:bg-slate-800 disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {action === "override" && (
            <div className="animate-fade-in space-y-3">
              <p className="text-xs text-slate-500">
                Your edited narrative replaces the Critic&apos;s output and is recorded as{" "}
                <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-slate-300">
                  produced_by: human
                </code>{" "}
                in the audit log.
              </p>

              {/* View toggle: raw editor / diff vs original / rendered preview */}
              <div className="flex items-center justify-between gap-2 border-b border-slate-700/60 pb-2">
                <div className="flex gap-1">
                  <EditViewButton active={editView === "edit"} onClick={() => setEditView("edit")}>
                    Edit
                  </EditViewButton>
                  <EditViewButton active={editView === "diff"} onClick={() => setEditView("diff")}>
                    Diff
                  </EditViewButton>
                  <EditViewButton
                    active={editView === "preview"}
                    onClick={() => setEditView("preview")}
                  >
                    Preview
                  </EditViewButton>
                </div>
                {/* Diff stats — visible from any view so the user always knows
                    how much they have changed. */}
                <div className="flex items-center gap-3 text-[11px] text-slate-500">
                  <span className="text-emerald-400">+{stats.added}</span>
                  <span className="text-red-400">−{stats.removed}</span>
                </div>
              </div>

              {editView === "edit" && (
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="block text-xs font-medium uppercase tracking-wider text-slate-400">
                      Markdown source
                    </label>
                    <textarea
                      className="h-72 w-full rounded-lg border border-slate-700 bg-slate-900/80 p-3 font-mono text-xs leading-relaxed text-slate-200 placeholder-slate-600 transition-colors focus:border-blue-500/50 focus:outline-none focus:ring-1 focus:ring-blue-500/30"
                      value={editContent}
                      onChange={(e) => setEditContent(e.target.value)}
                      autoFocus
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="block text-xs font-medium uppercase tracking-wider text-slate-400">
                      Live preview
                    </label>
                    <div className="h-72 overflow-y-auto rounded-lg border border-slate-700 bg-[#0a0f1e] p-3">
                      {editContent.trim() ? (
                        <Markdown content={editContent} />
                      ) : (
                        <p className="text-xs text-slate-600">Preview appears here…</p>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {editView === "diff" && <DiffPane ops={diffOps} />}

              {editView === "preview" && (
                <div className="h-96 overflow-y-auto rounded-lg border border-slate-700 bg-[#0a0f1e] p-4">
                  {editContent.trim() ? (
                    <Markdown content={editContent} />
                  ) : (
                    <p className="text-xs text-slate-600">Nothing to preview yet.</p>
                  )}
                </div>
              )}

              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={handleOverrideSubmit}
                  disabled={busy || !editContent.trim()}
                  className="rounded-lg border border-blue-500/30 bg-blue-500/10 px-4 py-2 text-sm font-medium text-blue-400 transition-all hover:bg-blue-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy ? "Working…" : "Save & approve"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAction("idle");
                    setEditContent("");
                  }}
                  disabled={busy}
                  className="rounded-lg border border-slate-700 px-4 py-2 text-sm font-medium text-slate-400 transition-all hover:bg-slate-800 disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-t-lg px-3.5 py-2 text-xs font-medium transition-colors",
        active
          ? "bg-violet-500/10 text-violet-300 ring-1 ring-inset ring-violet-500/20"
          : "text-slate-500 hover:text-slate-300",
      )}
    >
      {children}
    </button>
  );
}

/** Compact pill button used by the Edit/Diff/Preview view switcher. */
function EditViewButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-md px-2.5 py-1 text-[11px] font-medium uppercase tracking-wider transition-colors",
        active
          ? "bg-blue-500/15 text-blue-300 ring-1 ring-inset ring-blue-500/30"
          : "text-slate-500 hover:text-slate-300",
      )}
    >
      {children}
    </button>
  );
}

/** Line-by-line diff view — green for added, red for removed, grey for kept. */
function DiffPane({ ops }: { ops: DiffOp[] }) {
  if (ops.length === 0) {
    return (
      <p className="rounded-lg border border-slate-700 bg-[#0a0f1e] px-4 py-6 text-center text-xs text-slate-500">
        Nothing to compare yet.
      </p>
    );
  }
  return (
    <div className="h-96 overflow-y-auto rounded-lg border border-slate-700 bg-[#0a0f1e] font-mono text-[11px] leading-relaxed">
      {ops.map((op, i) => {
        if (op.type === "keep") {
          return (
            <div
              key={i}
              className="flex gap-3 border-b border-slate-800/40 px-3 py-0.5 text-slate-500"
            >
              <span className="w-3 shrink-0 text-slate-700"> </span>
              <span className="whitespace-pre-wrap break-words">
                {op.edited || " "}
              </span>
            </div>
          );
        }
        if (op.type === "add") {
          return (
            <div
              key={i}
              className="flex gap-3 border-b border-slate-800/40 bg-emerald-500/10 px-3 py-0.5 text-emerald-300"
            >
              <span className="w-3 shrink-0 select-none text-emerald-500">+</span>
              <span className="whitespace-pre-wrap break-words">
                {op.edited || " "}
              </span>
            </div>
          );
        }
        return (
          <div
            key={i}
            className="flex gap-3 border-b border-slate-800/40 bg-red-500/10 px-3 py-0.5 text-red-300/90"
          >
            <span className="w-3 shrink-0 select-none text-red-500">−</span>
            <span className="whitespace-pre-wrap break-words line-through decoration-red-500/40">
              {op.original || " "}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** The "Paper" identity cell — title first, authors + citation key beneath. */
function PaperIdentity({
  citationKey,
  paper,
  accent,
}: {
  citationKey: string;
  paper: Paper | undefined;
  accent: "violet" | "amber";
}) {
  const title = paper?.title?.trim();
  const authors = paper?.authors ?? [];
  const authorLine =
    authors.length > 0
      ? `${authors.slice(0, 3).join(", ")}${authors.length > 3 ? " et al." : ""}${
          paper?.year ? ` · ${paper.year}` : ""
        }`
      : null;
  const keyClass = accent === "violet" ? "text-violet-300" : "text-amber-300";

  return (
    <div className="space-y-1">
      {/* Title is the primary identifier — never just the citation key. */}
      <p className="font-medium leading-snug text-slate-200">
        {title || citationKey || "Untitled paper"}
      </p>
      {authorLine && <p className="text-[11px] leading-tight text-slate-500">{authorLine}</p>}
      {citationKey && (
        <code className={cn("font-mono text-[10px]", keyClass)}>{citationKey}</code>
      )}
    </div>
  );
}

function MatrixTable({
  rows,
  paperByKey,
}: {
  rows: PaperRow[];
  paperByKey: Map<string, Paper>;
}) {
  const okRows = rows.filter((r) => !r.extraction_failed);
  const failedRows = rows.filter((r) => r.extraction_failed);

  return (
    <div className="space-y-3">
      {/* Comparison grid — only the papers that extracted cleanly. */}
      {okRows.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-[#1e2d45]">
          <table className="w-full table-fixed border-collapse text-xs">
            <colgroup>
              <col className="w-56" />
              {MATRIX_COLUMNS.map((c) => (
                <col key={c.key} className="w-48" />
              ))}
            </colgroup>
            <thead>
              <tr className="bg-[#0a0f1e]">
                <th className="sticky left-0 z-10 border-b border-r border-[#1e2d45] bg-[#0a0f1e] px-3 py-2.5 text-left font-semibold uppercase tracking-wider text-slate-400">
                  Paper
                </th>
                {MATRIX_COLUMNS.map((col) => (
                  <th
                    key={col.key}
                    className="border-b border-[#1e2d45] px-3 py-2.5 text-left font-semibold uppercase tracking-wider text-slate-400"
                  >
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {okRows.map((row, i) => (
                <tr
                  key={row.citation_key || i}
                  className="align-top transition-colors hover:bg-[#1a2236]"
                >
                  <td className="sticky left-0 z-10 border-b border-r border-[#1a2236] bg-[#111827] px-3 py-3">
                    <PaperIdentity
                      citationKey={row.citation_key}
                      paper={paperByKey.get(row.citation_key)}
                      accent="violet"
                    />
                  </td>
                  {MATRIX_COLUMNS.map((col) => (
                    <td
                      key={col.key}
                      className="border-b border-[#1a2236] px-3 py-3 leading-relaxed text-slate-400"
                    >
                      {String(row[col.key] || "—")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="rounded-lg border border-[#1e2d45] bg-[#0a0f1e] px-4 py-6 text-center text-sm text-slate-500">
          No papers were successfully extracted — the comparison grid is empty.
        </p>
      )}

      {/* Failed papers — one classified card per failure, with a kind tag,
          a plain-English message, and an actionable hint. The previous
          version dumped the raw Gemini JSON which was unreadable. */}
      {failedRows.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-amber-500/20 bg-amber-500/5">
          <div className="border-b border-amber-500/15 px-4 py-2.5">
            <p className="text-xs font-semibold uppercase tracking-wider text-amber-400">
              {failedRows.length} paper{failedRows.length !== 1 ? "s" : ""} could not be extracted
            </p>
          </div>
          <ul className="divide-y divide-amber-500/10">
            {failedRows.map((row, i) => (
              <FailedPaperCard
                key={row.citation_key || i}
                citationKey={row.citation_key}
                paper={paperByKey.get(row.citation_key)}
                error={row.error}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FailedPaperCard — categorized error display per failed extraction
// ---------------------------------------------------------------------------

const ERROR_KIND_STYLES: Record<
  ErrorKind,
  { tag: string; label: string; dot: string }
> = {
  quota: {
    tag: "border-red-500/30 bg-red-500/10 text-red-300",
    label: "Quota exhausted",
    dot: "bg-red-400",
  },
  overload: {
    tag: "border-amber-500/30 bg-amber-500/10 text-amber-300",
    label: "Model overloaded",
    dot: "bg-amber-400",
  },
  auth: {
    tag: "border-red-500/30 bg-red-500/10 text-red-300",
    label: "Auth failure",
    dot: "bg-red-400",
  },
  schema: {
    tag: "border-slate-500/30 bg-slate-500/10 text-slate-300",
    label: "Input rejected",
    dot: "bg-slate-400",
  },
  network: {
    tag: "border-blue-500/30 bg-blue-500/10 text-blue-300",
    label: "Network error",
    dot: "bg-blue-400",
  },
  unknown: {
    tag: "border-slate-500/30 bg-slate-500/10 text-slate-300",
    label: "Unknown error",
    dot: "bg-slate-400",
  },
};

function FailedPaperCard({
  citationKey,
  paper,
  error,
}: {
  citationKey: string;
  paper: Paper | undefined;
  error: string | null;
}) {
  const classified = classifyError(error);
  const styles = ERROR_KIND_STYLES[classified.kind];
  return (
    <li className="px-4 py-3.5">
      <div className="flex items-start justify-between gap-3">
        <PaperIdentity citationKey={citationKey} paper={paper} accent="amber" />
        <span
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
            styles.tag,
          )}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", styles.dot)} />
          {styles.label}
        </span>
      </div>
      <div className="mt-2 space-y-1">
        <p className="text-xs leading-relaxed text-slate-300">{classified.message}</p>
        <p className="text-[11px] leading-relaxed text-slate-500">{classified.hint}</p>
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// SynthesisReadOnly — same structured view (matrix + narrative) used by the
// approval gate, but without the approve/reject/edit panel. Used on the
// "done" screen after the user has approved Phase 2 so the final synthesis
// is rendered as a real table + prose, not a raw markdown dump.
// ---------------------------------------------------------------------------

export function SynthesisReadOnly({
  matrix,
  summary,
  papers,
}: {
  matrix: Artifact | null;
  summary: Artifact | null;
  papers: Paper[];
}) {
  const parsedMatrix = useMemo(
    () => (matrix ? parseMatrix(matrix.content) : null),
    [matrix],
  );
  const paperByKey = useMemo(() => {
    const map = new Map<string, Paper>();
    for (const p of papers) map.set(p.citation_key, p);
    return map;
  }, [papers]);

  const summaryContent = summary?.content ?? "";
  const synthesisIdx = summaryContent.search(/##\s*Synthesis\b/i);
  // Split the summary at "## Synthesis": the part before is the matrix
  // (markdown table), the part from "## Synthesis" onward is the narrative.
  // After an override the user may have edited the matrix-portion markdown
  // directly; we MUST render that edited markdown instead of the original
  // matrix JSON artifact, otherwise saved edits look like they were dropped
  // (coderabbit PR #5 finding G2).
  const matrixMarkdown =
    synthesisIdx >= 0 ? summaryContent.slice(0, synthesisIdx).trim() : "";
  const narrative =
    synthesisIdx >= 0 ? summaryContent.slice(synthesisIdx) : summaryContent;

  // Heuristic: the matrix portion is considered "user-overridden" when it
  // contains a GFM table (a "| --- |" separator row). In that case we trust
  // the markdown as the source of truth and render it through react-markdown.
  // Otherwise — fresh Critic output, or no matrix portion at all — fall back
  // to the structured MatrixTable driven by the JSON matrix artifact.
  const matrixHasTable = /\n\s*\|\s*[-:|]+(\s*\|\s*[-:|]+)+\s*\|/.test(
    "\n" + matrixMarkdown,
  );

  if (!matrix && !summary) {
    return (
      <p className="rounded-lg border border-[#1e2d45] bg-[#0a0f1e] px-4 py-6 text-center text-sm text-slate-500">
        No synthesis artifacts to display.
      </p>
    );
  }

  return (
    <div className="space-y-5">
      {matrixHasTable ? (
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
            Comparison matrix
          </h3>
          <div className="rounded-lg border border-[#1e2d45] bg-[#0a0f1e] px-4 py-4">
            <Markdown content={matrixMarkdown} />
          </div>
        </section>
      ) : (
        parsedMatrix &&
        parsedMatrix.rows.length > 0 && (
          <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
              Comparison matrix
            </h3>
            <MatrixTable rows={parsedMatrix.rows} paperByKey={paperByKey} />
          </section>
        )
      )}

      {narrative.trim() && (
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
            Narrative
          </h3>
          <div className="rounded-lg border border-[#1e2d45] bg-[#0a0f1e] px-4 py-4">
            <Markdown content={narrative} />
          </div>
        </section>
      )}
    </div>
  );
}
