"use client";

import { useMemo, useState } from "react";

import { diffLines, diffStats, type DiffOp } from "@/components/workflow/diffLines";
import { Markdown } from "@/components/workflow/Markdown";
import type { Artifact, SectionName } from "@/lib/types";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Canonical seven-section order — mirrors backend SectionName.
// Used to compute the "Section N of 7" progress chip.
// ---------------------------------------------------------------------------

const CANONICAL: SectionName[] = [
  "abstract",
  "introduction",
  "related_work",
  "methodology",
  "results",
  "discussion",
  "conclusion",
];

const SECTION_LABELS: Record<SectionName, string> = {
  abstract: "Abstract",
  introduction: "Introduction",
  related_work: "Related Work",
  methodology: "Methodology",
  results: "Results",
  discussion: "Discussion",
  conclusion: "Conclusion",
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface SectionOverridePayload {
  artifact_kind: "section";
  label: string;
  content: string;
  mime_type: string;
}

interface SectionReviewProps {
  section: Artifact | null;
  /** Current section name from the WS `approval.required` event. */
  currentSection: SectionName | null;
  /** Citation keys returned by the Scribe; offenders carry an `INVALID:` prefix. */
  citedKeys?: string[];
  loading: boolean;
  busy: boolean;
  onApprove: () => void;
  onReject: (feedback: string) => void;
  onOverride: (payload: SectionOverridePayload) => void;
}

type Tab = "preview" | "source" | "citations";
type Action = "idle" | "reject" | "override";
type EditView = "edit" | "diff" | "preview";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SectionReview({
  section,
  currentSection,
  citedKeys,
  loading,
  busy,
  onApprove,
  onReject,
  onOverride,
}: SectionReviewProps) {
  const [tab, setTab] = useState<Tab>("preview");
  const [action, setAction] = useState<Action>("idle");
  const [feedback, setFeedback] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editView, setEditView] = useState<EditView>("edit");

  const sectionName = currentSection ?? (section?.label as SectionName | undefined) ?? null;
  const progress = useMemo(() => {
    if (!sectionName) return null;
    const idx = CANONICAL.indexOf(sectionName);
    if (idx < 0) return null;
    return { current: idx + 1, total: CANONICAL.length };
  }, [sectionName]);

  const sectionContent = section?.content ?? "";

  // Diff is computed only in override mode so the LCS table is cheap.
  const diffOps = useMemo(
    () => (action === "override" ? diffLines(sectionContent, editContent) : []),
    [action, sectionContent, editContent],
  );
  const stats = useMemo(() => diffStats(diffOps), [diffOps]);

  // Citation chips: keys starting with `INVALID:` are the second-failure
  // offenders flagged by the Scribe (see backend agents/scribe.py).
  const { validCitations, invalidCitations } = useMemo(() => {
    const valid: string[] = [];
    const invalid: string[] = [];
    for (const k of citedKeys ?? []) {
      if (k.startsWith("INVALID:")) invalid.push(k.slice("INVALID:".length));
      else valid.push(k);
    }
    return { validCitations: valid, invalidCitations: invalid };
  }, [citedKeys]);

  function startEditing() {
    setEditContent(sectionContent);
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
      artifact_kind: "section",
      label: section?.label ?? sectionName ?? "section",
      content: editContent.trim(),
      mime_type: "text/markdown",
    });
  }

  // ── Loading — manuscript-shaped skeleton, not a spinner ───────────────
  if (loading) {
    return (
      <div className="space-y-5 animate-fade-in">
        <div className="space-y-2">
          <div className="skeleton h-6 w-56" />
          <div className="skeleton h-3 w-64" />
        </div>
        <div className="space-y-3 pt-2">
          <div className="skeleton h-4 w-full" />
          <div className="skeleton h-4 w-[92%]" />
          <div className="skeleton h-4 w-[97%]" />
          <div className="skeleton h-4 w-3/4" />
          <div className="skeleton h-4 w-[88%]" />
          <div className="skeleton h-4 w-1/2" />
        </div>
        <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
          Scribe is writing {sectionName ? SECTION_LABELS[sectionName] : "the section"}…
        </p>
      </div>
    );
  }

  if (!section) {
    return (
      <div className="py-12 text-center">
        <p className="text-sm text-muted">No section draft yet.</p>
        <p className="mt-1 text-xs text-muted-foreground">The Scribe may still be working.</p>
      </div>
    );
  }

  return (
    <div className="space-y-8 animate-fade-in">
      {/* ── Section — borderless; whitespace + a tab row carry structure. ── */}
      <div>
        {/* Header */}
        <div className="flex items-center justify-between pb-4">
          <div>
            <h2 className="flex items-center gap-2 font-display text-lg font-bold text-foreground">
              <span className="flex h-5 w-5 items-center justify-center rounded bg-primary/15 font-mono text-[10px] text-primary">
                ✍
              </span>
              {sectionName ? SECTION_LABELS[sectionName] : "Section"}
            </h2>
            <p className="mt-1 text-xs text-muted">
              {progress
                ? `Section ${progress.current} of ${progress.total} · the Scribe drafts one section at a time.`
                : "The Scribe drafts one section at a time."}
            </p>
          </div>
          {progress && (
            <div className="flex items-center gap-1.5">
              {CANONICAL.map((s, i) => {
                const state =
                  i < progress.current - 1 ? "done" : i === progress.current - 1 ? "current" : "todo";
                return (
                  <span
                    key={s}
                    title={SECTION_LABELS[s]}
                    className={cn(
                      "h-1.5 w-5 rounded-full transition-colors",
                      state === "done" && "bg-primary/60",
                      state === "current" && "bg-primary shadow-[0_0_8px_oklch(72%_0.20_155_/_0.5)]",
                      state === "todo" && "bg-surface-elevated",
                    )}
                  />
                );
              })}
            </div>
          )}
        </div>

        {/* Invalid-citation warning banner (Scribe surfaced offenders after retry) */}
        {invalidCitations.length > 0 && (
          <div className="mb-4 border-l-2 border-warning py-3 pl-4">
            <div className="flex items-start gap-2.5">
              <svg
                className="mt-0.5 h-4 w-4 shrink-0 text-warning"
                viewBox="0 0 16 16"
                fill="none"
              >
                <path d="M8 5v4M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
              </svg>
              <div className="space-y-0.5">
                <p className="text-xs font-semibold text-warning">
                  {invalidCitations.length} citation{invalidCitations.length !== 1 ? "s" : ""} not in
                  the approved pool
                </p>
                <p className="text-xs text-muted">
                  The Scribe retried once and still cited{" "}
                  <code className="rounded bg-surface-elevated px-1 py-0.5 font-mono text-warning">
                    {invalidCitations.join(", ")}
                  </code>
                  . Review the citations tab; you can reject to regenerate or edit to fix the keys.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-border pt-2">
          <TabButton active={tab === "preview"} onClick={() => setTab("preview")}>
            Preview
          </TabButton>
          <TabButton active={tab === "source"} onClick={() => setTab("source")}>
            Source
          </TabButton>
          <TabButton active={tab === "citations"} onClick={() => setTab("citations")}>
            Citations ({(validCitations.length + invalidCitations.length).toString()})
          </TabButton>
        </div>

        {/* Tab body */}
        <div className="py-6">
          {tab === "preview" && (
            <div className="mx-auto max-w-[68ch]">
              <Markdown content={sectionContent} variant="prose" />
            </div>
          )}

          {tab === "source" && (
            <pre className="overflow-x-auto rounded-lg bg-surface-elevated p-4 font-mono text-xs leading-relaxed text-foreground">
              {sectionContent}
            </pre>
          )}

          {tab === "citations" && (
            <div className="space-y-3">
              {validCitations.length === 0 && invalidCitations.length === 0 && (
                <p className="text-xs text-muted">
                  No citations were detected in this section.
                </p>
              )}
              {validCitations.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                    In the approved pool ({validCitations.length})
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {validCitations.map((k) => (
                      <code
                        key={k}
                        className="rounded-full border border-primary/30 bg-primary/10 px-2.5 py-0.5 font-mono text-[11px] text-primary"
                      >
                        {k}
                      </code>
                    ))}
                  </div>
                </div>
              )}
              {invalidCitations.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-warning">
                    Not in pool — flagged by Scribe ({invalidCitations.length})
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {invalidCitations.map((k) => (
                      <code
                        key={k}
                        className="rounded-full border border-warning/40 bg-warning/10 px-2.5 py-0.5 font-mono text-[11px] text-warning"
                      >
                        {k}
                      </code>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Approval panel — borderless review gate (left rule + glow). ──── */}
      <div className="glow-emerald border-l-2 border-primary-dim pl-5">
        <div className="flex items-center gap-2.5 pb-4">
          <span className="animate-pulse-dot flex h-2 w-2 rounded-full bg-primary" />
          <p className="font-display text-base font-bold text-primary">
            Review the {sectionName ? SECTION_LABELS[sectionName] : "section"}
          </p>
        </div>

        <div className="space-y-4">
          {action === "idle" && (
            <>
              <p className="text-sm text-muted">
                Approve to advance to the next section, reject with feedback to regenerate,
                or edit the draft directly.
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
                      Approve &amp; next
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
                    <path d="M8 3v5M8 10v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  </svg>
                  Reject &amp; regenerate
                </button>
                <button
                  type="button"
                  onClick={startEditing}
                  disabled={busy || !section}
                  className="flex items-center gap-2 rounded-lg border border-border bg-surface-elevated/50 px-4 py-2 text-sm font-medium text-foreground transition-all  hover:bg-surface-elevated disabled:cursor-not-allowed disabled:opacity-40"
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
                  Edit &amp; override
                </button>
              </div>
            </>
          )}

          {action === "reject" && (
            <div className="space-y-3 animate-fade-in">
              <label className="block text-xs font-medium uppercase tracking-wider text-muted">
                Feedback for the Scribe
              </label>
              <textarea
                className="w-full rounded-lg border border-border bg-surface-elevated p-3 text-sm text-foreground placeholder-muted-foreground/50 transition-colors focus:border-primary-dim/60 focus:outline-none focus:ring-1 focus:ring-primary-dim/30"
                rows={3}
                placeholder="e.g. Shorten to 200 words; lead with the headline finding…"
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
                Your edited section replaces the Scribe&apos;s draft and is recorded as{" "}
                <code className="rounded bg-surface-elevated px-1.5 py-0.5 font-mono text-foreground">
                  produced_by: human
                </code>{" "}
                in the audit log. Approval is implied — the workflow advances to the next section.
              </p>

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
                  {busy ? "Working…" : "Save & advance"}
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
// Sub-components (mirror SynthesisReview)
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
        // Underline-indicator tab (borderless), matching SynthesisReview.
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
            <div key={i} className="flex gap-3 border-b border-border px-3 py-0.5 text-muted">
              <span className="w-3 shrink-0 text-muted-foreground"> </span>
              <span className="whitespace-pre-wrap break-words">{op.edited || " "}</span>
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
              <span className="whitespace-pre-wrap break-words">{op.edited || " "}</span>
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
              {op.original || " "}
            </span>
          </div>
        );
      })}
    </div>
  );
}
