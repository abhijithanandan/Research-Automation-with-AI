"use client";

import { useMemo, useState } from "react";

import { diffLines, diffStats, type DiffOp } from "@/components/workflow/diffLines";
import { Markdown } from "@/components/workflow/Markdown";
import { MatrixModal } from "@/components/workflow/MatrixModal";
import type { Artifact, Paper } from "@/lib/types";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Matrix shape — mirrors backend app/agents/critic.py MatrixModel / PaperExtraction
// ---------------------------------------------------------------------------

// Re-exported so MatrixModal can build a typed Map<string, Paper> + render
// the same `MatrixTable` without duplicating the shape definitions.
export interface PaperRow {
  citation_key: string;
  problem: string;
  method: string;
  dataset: string;
  key_findings: string;
  limitations: string;
  extraction_failed: boolean;
  error: string | null;
}

export interface MatrixModel {
  rows: PaperRow[];
}

export const MATRIX_COLUMNS: { key: keyof PaperRow; label: string }[] = [
  { key: "problem", label: "Problem" },
  { key: "method", label: "Method" },
  { key: "dataset", label: "Dataset" },
  { key: "key_findings", label: "Key findings" },
  { key: "limitations", label: "Limitations" },
];

export function parseMatrix(content: string): MatrixModel | null {
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
  // Fullscreen matrix view — mounts MatrixModal into document.body so the
  // table can use the full monitor width instead of the page's max-w cap.
  const [matrixExpanded, setMatrixExpanded] = useState(false);

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

  // ── Loading — skeleton, not a spinner ─────────────────────────────────
  // Mirror the eventual layout (header line, tab row, matrix grid) so the
  // transition into real content has no jump (ui-ux-polish loading patterns).
  if (loading) {
    return (
      <div className="space-y-5 animate-fade-in">
        <div className="space-y-2">
          <div className="skeleton h-5 w-48" />
          <div className="skeleton h-3 w-72" />
        </div>
        <div className="flex gap-3">
          <div className="skeleton h-7 w-24" />
          <div className="skeleton h-7 w-36" />
        </div>
        <div className="space-y-2.5">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="skeleton h-10 w-full" />
          ))}
        </div>
        <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
          Critic is synthesizing…
        </p>
      </div>
    );
  }

  // ── Empty (artifacts not yet available) ───────────────────────────────
  if (!matrix && !summary) {
    return (
      <div className="py-12 text-center">
        <p className="text-sm text-muted">No synthesis artifacts yet.</p>
        <p className="mt-1 text-xs text-muted-foreground">The Critic may still be working.</p>
      </div>
    );
  }

  return (
    <div className="space-y-8 animate-fade-in">
      {/* ── Synthesis section — borderless; whitespace + a tab row carry the
          structure instead of a card box. ─────────────────────────────── */}
      <div>
        {/* Header */}
        <div className="flex items-center justify-between pb-4">
          <div>
            <h2 className="flex items-center gap-2 font-display text-lg font-bold text-foreground">
              <span className="flex h-5 w-5 items-center justify-center rounded bg-primary/15 font-mono text-[10px] text-primary">
                02
              </span>
              Literature synthesis
            </h2>
            <p className="mt-1 text-xs text-muted">
              The Critic compared {paperCount} paper{paperCount !== 1 ? "s" : ""} and wrote a narrative review.
            </p>
          </div>
          {failedCount > 0 && (
            <span
              className={cn(
                "shrink-0 rounded-full border px-3 py-1 text-xs font-medium",
                allFailed
                  ? "border-destructive/30 bg-destructive/10 text-destructive"
                  : "border-warning/20 bg-warning/10 text-warning",
              )}
            >
              {failedCount}/{paperCount} extraction{failedCount !== 1 ? "s" : ""} failed
            </span>
          )}
        </div>

        {/* Failure banner — surfaces the root cause without the JSON wall. */}
        {(allFailed || narrativeFailed) && (
          <div className="border-l-2 border-destructive py-3 pl-4">
            <div className="flex items-start gap-2.5">
              <svg
                className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
                viewBox="0 0 16 16"
                fill="none"
              >
                <path d="M8 5v4M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
              </svg>
              <div className="space-y-0.5">
                <p className="text-xs font-semibold text-destructive">
                  The Critic could not complete the synthesis
                </p>
                <p className="text-xs text-muted">
                  {humanizeError(
                    parsedMatrix?.rows.find((r) => r.extraction_failed)?.error ?? narrative,
                  )}{" "}
                  Use <span className="text-foreground">Reject &amp; regenerate</span> once the
                  provider quota recovers.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-border pt-2">
          <TabButton active={tab === "narrative"} onClick={() => setTab("narrative")}>
            Narrative
          </TabButton>
          <TabButton active={tab === "matrix"} onClick={() => setTab("matrix")}>
            Comparison matrix
          </TabButton>
        </div>

        {/* Tab body */}
        <div className="py-6">
          {tab === "narrative" &&
            (!narrative ? (
              <p className="text-sm text-muted">No narrative was produced.</p>
            ) : narrativeFailed ? (
              <div className="rounded-lg border border-destructive/20 bg-destructive/5 p-4">
                <p className="text-sm font-medium text-destructive">Narrative generation failed</p>
                <p className="mt-1 text-xs leading-relaxed text-muted">
                  {humanizeError(narrative)}
                </p>
              </div>
            ) : (
              <div className="mx-auto max-w-[68ch]">
                <Markdown content={narrative} variant="prose" />
              </div>
            ))}

          {tab === "matrix" &&
            (parsedMatrix && parsedMatrix.rows.length > 0 ? (
              <div className="space-y-3">
                {/* Expand button — pops the matrix into a fullscreen portal
                    so the user can read the table without the page's
                    max-w cap squeezing it into nested scrollbars. */}
                <div className="flex items-center justify-end">
                  <button
                    type="button"
                    onClick={() => setMatrixExpanded(true)}
                    className="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:border-primary hover:bg-primary/20"
                    aria-label="Expand matrix to fullscreen"
                  >
                    <svg className="h-3 w-3" viewBox="0 0 16 16" fill="none">
                      <path
                        d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                      />
                    </svg>
                    Expand to fullscreen
                  </button>
                </div>
                <MatrixTable rows={parsedMatrix.rows} paperByKey={paperByKey} />
              </div>
            ) : (
              <p className="text-sm text-muted">
                The comparison matrix could not be parsed.
              </p>
            ))}
        </div>
      </div>

      {/* Fullscreen matrix modal — only mounted when expanded. The component
          itself short-circuits the portal render when open=false so leaving
          it in the tree is cheap. */}
      <MatrixModal
        matrix={parsedMatrix}
        paperByKey={paperByKey}
        open={matrixExpanded}
        onClose={() => setMatrixExpanded(false)}
      />

      {/* ── Approval panel — borderless review gate: a left emerald rule +
          soft glow marks the awaiting-review state without a boxed card. ── */}
      <div className="glow-emerald border-l-2 border-primary-dim pl-5">
        <div className="flex items-center gap-2.5 pb-4">
          <span className="animate-pulse-dot flex h-2 w-2 rounded-full bg-primary" />
          <p className="font-display text-base font-bold text-primary">Review the synthesis</p>
        </div>

        <div className="space-y-4">
          {action === "idle" && (
            <>
              <p className="text-sm text-muted">
                Approve to advance to drafting, reject with feedback to regenerate, or
                edit the narrative directly.
              </p>
              <div className="flex flex-wrap gap-3">
                <button
                  type="button"
                  onClick={onApprove}
                  disabled={busy}
                  className="flex items-center gap-2 rounded-md bg-primary px-5 py-2.5 text-sm font-semibold text-primary-foreground transition-all duration-200 hover:bg-primary-hover hover:shadow-[0_0_20px_oklch(72%_0.20_155_/_0.35)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy ? (
                    <>
                      <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
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
                  className="flex items-center gap-2 rounded-lg border border-primary-dim/40 bg-primary-dim-bg px-4 py-2 text-sm font-medium text-primary transition-all hover:border-primary-dim/60 hover:bg-primary-dim-bg disabled:cursor-not-allowed disabled:opacity-40"
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
                  className="flex items-center gap-2 rounded-lg border border-border bg-surface-elevated px-4 py-2 text-sm font-medium text-foreground transition-all hover:bg-surface-elevated disabled:cursor-not-allowed disabled:opacity-40"
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
              <label className="block text-xs font-medium uppercase tracking-wider text-muted">
                Feedback for the Critic
              </label>
              <textarea
                className="w-full rounded-lg border border-border bg-surface-elevated p-3 text-sm text-foreground placeholder-muted-foreground/50 transition-colors focus:border-primary-dim/60 focus:outline-none focus:ring-1 focus:ring-primary-dim/30"
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
                  className="rounded-lg border border-primary-dim/40 bg-primary-dim-bg px-4 py-2 text-sm font-medium text-primary transition-all hover:border-primary-dim/60 hover:bg-primary-dim-bg disabled:cursor-not-allowed disabled:opacity-40"
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
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted transition-all hover:bg-surface-elevated disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {action === "override" && (
            <div className="animate-fade-in space-y-3">
              <p className="text-xs text-muted">
                Your edited narrative replaces the Critic&apos;s output and is recorded as{" "}
                <code className="rounded bg-surface-elevated px-1.5 py-0.5 font-mono text-foreground">
                  produced_by: human
                </code>{" "}
                in the audit log.
              </p>

              {/* View toggle: raw editor / diff vs original / rendered preview */}
              <div className="flex items-center justify-between gap-2 border-b border-border/60 pb-2">
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
                <div className="flex items-center gap-3 text-[11px] text-muted">
                  <span className="text-primary">+{stats.added}</span>
                  <span className="text-destructive">−{stats.removed}</span>
                </div>
              </div>

              {editView === "edit" && (
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="block text-xs font-medium uppercase tracking-wider text-muted">
                      Markdown source
                    </label>
                    <textarea
                      className="h-72 w-full rounded-lg border border-border bg-surface-elevated p-3 font-mono text-xs leading-relaxed text-foreground placeholder-muted-foreground/50 transition-colors focus:border-primary/60 focus:outline-none focus:ring-1 focus:ring-primary/30"
                      value={editContent}
                      onChange={(e) => setEditContent(e.target.value)}
                      autoFocus
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="block text-xs font-medium uppercase tracking-wider text-muted">
                      Live preview
                    </label>
                    <div className="h-72 overflow-y-auto rounded-lg bg-surface-elevated p-4">
                      {editContent.trim() ? (
                        <Markdown content={editContent} />
                      ) : (
                        <p className="text-xs text-muted-foreground">Preview appears here…</p>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {editView === "diff" && <DiffPane ops={diffOps} />}

              {editView === "preview" && (
                <div className="h-96 overflow-y-auto rounded-lg bg-surface-elevated p-4">
                  {editContent.trim() ? (
                    <Markdown content={editContent} />
                  ) : (
                    <p className="text-xs text-muted-foreground">Nothing to preview yet.</p>
                  )}
                </div>
              )}

              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={handleOverrideSubmit}
                  disabled={busy || !editContent.trim()}
                  className="rounded-lg border border-primary/40 bg-primary/10 px-4 py-2 text-sm font-medium text-primary transition-all hover:bg-primary/15 disabled:cursor-not-allowed disabled:opacity-40"
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
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted transition-all hover:bg-surface-elevated disabled:opacity-40"
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
        // Underline-indicator tab (borderless): the active tab is marked by an
        // emerald rule along its bottom edge, not a filled pill box.
        "-mb-px border-b-2 px-1 pb-2.5 text-xs font-medium transition-all duration-200",
        active
          ? "border-primary text-primary"
          : "border-transparent text-muted hover:text-foreground",
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
          ? "bg-primary/15 text-primary ring-1 ring-inset ring-primary/30"
          : "text-muted hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

/** Segmented toggle button for the matrix Comfortable/Compact density switch. */
function DensityButton({
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
      aria-pressed={active}
      className={cn(
        "rounded px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.12em] transition-all duration-150 ease-in-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60",
        active ? "bg-primary text-primary-foreground" : "text-muted hover:text-foreground",
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
      <p className="py-6 text-center text-xs text-muted">
        Nothing to compare yet.
      </p>
    );
  }
  return (
    <div className="h-96 overflow-y-auto rounded-lg bg-surface-elevated p-4 font-mono text-[11px] leading-relaxed">
      {ops.map((op, i) => {
        if (op.type === "keep") {
          return (
            <div
              key={i}
              className="flex gap-3 border-b border-border px-3 py-0.5 text-muted"
            >
              <span className="w-3 shrink-0 text-muted-foreground"> </span>
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
              className="flex gap-3 border-b border-border bg-primary/10 px-3 py-0.5 text-primary"
            >
              <span className="w-3 shrink-0 select-none text-primary">+</span>
              <span className="whitespace-pre-wrap break-words">
                {op.edited || " "}
              </span>
            </div>
          );
        }
        return (
          <div
            key={i}
            className="flex gap-3 border-b border-border bg-destructive/10 px-3 py-0.5 text-destructive/90"
          >
            <span className="w-3 shrink-0 select-none text-destructive">−</span>
            <span className="whitespace-pre-wrap break-words line-through decoration-destructive/40">
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
  const keyClass = accent === "violet" ? "text-primary" : "text-warning";

  return (
    <div className="space-y-1">
      {/* Title is the primary identifier — never just the citation key. */}
      <p className="font-medium leading-snug text-foreground">
        {title || citationKey || "Untitled paper"}
      </p>
      {authorLine && <p className="text-[11px] leading-tight text-muted">{authorLine}</p>}
      {citationKey && (
        <code className={cn("font-mono text-[10px]", keyClass)}>{citationKey}</code>
      )}
    </div>
  );
}

type Density = "comfortable" | "compact";

export function MatrixTable({
  rows,
  paperByKey,
}: {
  rows: PaperRow[];
  paperByKey: Map<string, Paper>;
}) {
  const okRows = rows.filter((r) => !r.extraction_failed);
  const failedRows = rows.filter((r) => r.extraction_failed);
  const [density, setDensity] = useState<Density>("comfortable");

  // Density drives only padding + line-height — never row *height* via fixed
  // values, so toggling can't cause a layout-shift jolt outside the table.
  const cellPad = density === "compact" ? "px-2.5 py-2" : "px-3 py-3.5";
  const headPad = density === "compact" ? "px-2.5 py-2" : "px-3 py-2.5";
  const cellLeading = density === "compact" ? "leading-snug" : "leading-relaxed";

  return (
    <div className="space-y-3">
      {/* Comparison grid — only the papers that extracted cleanly. Borderless:
          no box around the table. The header is sticky to the top and the
          first column is sticky to the left, so during scroll the user never
          loses which paper/attribute they're reading. */}
      {okRows.length > 0 ? (
        <>
          {/* Density toggle — sits above the grid, right-aligned, subtle. */}
          <div className="flex items-center justify-end gap-2">
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
              Density
            </span>
            <div className="inline-flex rounded-md bg-surface-elevated p-0.5">
              <DensityButton
                active={density === "comfortable"}
                onClick={() => setDensity("comfortable")}
              >
                Comfortable
              </DensityButton>
              <DensityButton
                active={density === "compact"}
                onClick={() => setDensity("compact")}
              >
                Compact
              </DensityButton>
            </div>
          </div>

          <div className="overflow-auto">
            <table className="w-full table-fixed border-collapse text-xs">
              <colgroup>
                <col className="w-56" />
                {MATRIX_COLUMNS.map((c) => (
                  <col key={c.key} className="w-48" />
                ))}
              </colgroup>
              <thead>
                <tr>
                  {/* Corner cell: sticky on BOTH axes (top + left), highest z so
                      it stays above both the sticky row and the sticky column. */}
                  <th
                    className={cn(
                      "sticky left-0 top-0 z-30 bg-surface-elevated text-left font-mono text-[10px] font-semibold uppercase tracking-[0.15em] text-muted-foreground",
                      headPad,
                    )}
                  >
                    Paper
                  </th>
                  {MATRIX_COLUMNS.map((col) => (
                    <th
                      key={col.key}
                      className={cn(
                        "sticky top-0 z-20 bg-surface-elevated text-left font-mono text-[10px] font-semibold uppercase tracking-[0.15em] text-muted-foreground",
                        headPad,
                      )}
                    >
                      {col.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {okRows.map((row, i) => (
                  <tr
                    key={row.citation_key || i}
                    // Buttery row tracking: a subtle emerald wash follows the
                    // pointer. group/row lets the sticky first cell adopt the
                    // same highlight so the row reads as one continuous band.
                    className="group/row align-top transition-colors duration-150 ease-in-out hover:bg-primary/[0.06]"
                  >
                    <td
                      className={cn(
                        "sticky left-0 z-10 bg-background transition-colors duration-150 ease-in-out group-hover/row:bg-[oklch(8%_0.01_155)]",
                        cellPad,
                      )}
                    >
                      <PaperIdentity
                        citationKey={row.citation_key}
                        paper={paperByKey.get(row.citation_key)}
                        accent="violet"
                      />
                    </td>
                    {MATRIX_COLUMNS.map((col) => (
                      <td key={col.key} className={cn("text-muted", cellPad, cellLeading)}>
                        {String(row[col.key] || "—")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <p className="py-6 text-center text-sm text-muted">
          No papers were successfully extracted — the comparison grid is empty.
        </p>
      )}

      {/* Failed papers — one classified card per failure, with a kind tag,
          a plain-English message, and an actionable hint. The previous
          version dumped the raw Gemini JSON which was unreadable. */}
      {failedRows.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-warning/20 bg-warning/5">
          <div className="border-b border-warning/15 px-4 py-2.5">
            <p className="text-xs font-semibold uppercase tracking-wider text-warning">
              {failedRows.length} paper{failedRows.length !== 1 ? "s" : ""} could not be extracted
            </p>
          </div>
          <ul className="divide-y divide-warning/10">
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
    tag: "border-destructive/30 bg-destructive/10 text-destructive",
    label: "Quota exhausted",
    dot: "bg-destructive",
  },
  overload: {
    tag: "border-warning/30 bg-warning/10 text-warning",
    label: "Model overloaded",
    dot: "bg-warning",
  },
  auth: {
    tag: "border-destructive/30 bg-destructive/10 text-destructive",
    label: "Auth failure",
    dot: "bg-destructive",
  },
  schema: {
    tag: "border-border bg-surface-elevated text-foreground",
    label: "Input rejected",
    dot: "bg-muted",
  },
  network: {
    tag: "border-primary/30 bg-primary/10 text-primary",
    label: "Network error",
    dot: "bg-primary",
  },
  unknown: {
    tag: "border-border bg-surface-elevated text-foreground",
    label: "Unknown error",
    dot: "bg-muted",
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
        <p className="text-xs leading-relaxed text-foreground">{classified.message}</p>
        <p className="text-[11px] leading-relaxed text-muted">{classified.hint}</p>
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
  const [matrixExpanded, setMatrixExpanded] = useState(false);
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
      <p className="py-6 text-center text-sm text-muted">
        No synthesis artifacts to display.
      </p>
    );
  }

  return (
    <div className="space-y-5">
      {matrixHasTable ? (
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">
            Comparison matrix
          </h3>
          <div className="rounded-lg bg-surface-elevated px-4 py-4">
            <Markdown content={matrixMarkdown} />
          </div>
        </section>
      ) : (
        parsedMatrix &&
        parsedMatrix.rows.length > 0 && (
          <section>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
                Comparison matrix
              </h3>
              <button
                type="button"
                onClick={() => setMatrixExpanded(true)}
                className="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:border-primary hover:bg-primary/20"
                aria-label="Expand matrix to fullscreen"
              >
                <svg className="h-3 w-3" viewBox="0 0 16 16" fill="none">
                  <path
                    d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                  />
                </svg>
                Expand to fullscreen
              </button>
            </div>
            <MatrixTable rows={parsedMatrix.rows} paperByKey={paperByKey} />
          </section>
        )
      )}

      {narrative.trim() && (
        <section>
          <h3 className="mb-3 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Narrative
          </h3>
          <div className="mx-auto max-w-[68ch]">
            <Markdown content={narrative} variant="prose" />
          </div>
        </section>
      )}

      <MatrixModal
        matrix={parsedMatrix}
        paperByKey={paperByKey}
        open={matrixExpanded}
        onClose={() => setMatrixExpanded(false)}
      />
    </div>
  );
}
