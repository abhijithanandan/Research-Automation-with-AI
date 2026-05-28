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

  // ── Loading ───────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-border bg-background px-5 py-4 text-sm text-slate-500">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-emerald-500" />
        Scribe is writing {sectionName ? SECTION_LABELS[sectionName] : "the section"}…
      </div>
    );
  }

  if (!section) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-border bg-background py-10 text-center">
        <span className="text-2xl">✍️</span>
        <p className="text-sm text-slate-500">No section draft yet.</p>
        <p className="text-xs text-slate-600">The Scribe may still be working.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4 animate-fade-in">
      {/* ── Section card ──────────────────────────────────────────────── */}
      <div className="overflow-hidden rounded-xl border border-emerald-500/20 bg-background">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <span className="flex h-5 w-5 items-center justify-center rounded bg-emerald-500/20 text-[10px] text-emerald-300">
                ✍
              </span>
              {sectionName ? SECTION_LABELS[sectionName] : "Section"}
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
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
                      state === "done" && "bg-emerald-500/60",
                      state === "current" && "bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.5)]",
                      state === "todo" && "bg-slate-700",
                    )}
                  />
                );
              })}
            </div>
          )}
        </div>

        {/* Invalid-citation warning banner (Scribe surfaced offenders after retry) */}
        {invalidCitations.length > 0 && (
          <div className="border-b border-amber-500/20 bg-amber-500/5 px-5 py-3">
            <div className="flex items-start gap-2.5">
              <svg
                className="mt-0.5 h-4 w-4 shrink-0 text-amber-400"
                viewBox="0 0 16 16"
                fill="none"
              >
                <path d="M8 5v4M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
              </svg>
              <div className="space-y-0.5">
                <p className="text-xs font-semibold text-amber-300">
                  {invalidCitations.length} citation{invalidCitations.length !== 1 ? "s" : ""} not in
                  the approved pool
                </p>
                <p className="text-xs text-slate-400">
                  The Scribe retried once and still cited{" "}
                  <code className="rounded bg-slate-800 px-1 py-0.5 font-mono text-amber-300">
                    {invalidCitations.join(", ")}
                  </code>
                  . Review the citations tab; you can reject to regenerate or edit to fix the keys.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-border px-3 pt-3">
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
        <div className="px-5 py-5">
          {tab === "preview" && <Markdown content={sectionContent} />}

          {tab === "source" && (
            <pre className="overflow-x-auto rounded-lg border border-border bg-background p-3 font-mono text-xs leading-relaxed text-slate-300">
              {sectionContent}
            </pre>
          )}

          {tab === "citations" && (
            <div className="space-y-3">
              {validCitations.length === 0 && invalidCitations.length === 0 && (
                <p className="text-xs text-slate-500">
                  No citations were detected in this section.
                </p>
              )}
              {validCitations.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                    In the approved pool ({validCitations.length})
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {validCitations.map((k) => (
                      <code
                        key={k}
                        className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-0.5 font-mono text-[11px] text-emerald-300"
                      >
                        {k}
                      </code>
                    ))}
                  </div>
                </div>
              )}
              {invalidCitations.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-amber-400">
                    Not in pool — flagged by Scribe ({invalidCitations.length})
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {invalidCitations.map((k) => (
                      <code
                        key={k}
                        className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 font-mono text-[11px] text-amber-300"
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

      {/* ── Approval panel ─────────────────────────────────────────────── */}
      <div className="glow-emerald overflow-hidden rounded-xl border border-emerald-700/40 bg-emerald-800/10">
        <div className="flex items-center gap-3 border-b border-emerald-700/40 px-5 py-4">
          <span className="animate-pulse-dot flex h-2 w-2 rounded-full bg-emerald-400" />
          <p className="text-sm font-semibold text-emerald-300">
            Review the {sectionName ? SECTION_LABELS[sectionName] : "section"}
          </p>
        </div>

        <div className="space-y-4 px-5 py-4">
          {action === "idle" && (
            <>
              <p className="text-sm text-slate-400">
                Approve to advance to the next section, reject with feedback to regenerate,
                or edit the draft directly.
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
                      Approve &amp; next
                    </>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setAction("reject")}
                  disabled={busy}
                  className="flex items-center gap-2 rounded-lg border border-emerald-700/40 bg-emerald-800/10 px-4 py-2 text-sm font-medium text-emerald-300 transition-all hover:border-emerald-700/60 hover:bg-emerald-800/20 disabled:cursor-not-allowed disabled:opacity-40"
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
                  Edit &amp; override
                </button>
              </div>
            </>
          )}

          {action === "reject" && (
            <div className="space-y-3 animate-fade-in">
              <label className="block text-xs font-medium uppercase tracking-wider text-slate-400">
                Feedback for the Scribe
              </label>
              <textarea
                className="w-full rounded-lg border border-border bg-slate-900/80 p-3 text-sm text-slate-200 placeholder-slate-600 transition-colors focus:border-emerald-700/60 focus:outline-none focus:ring-1 focus:ring-emerald-700/30"
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
                  className="rounded-lg border border-emerald-700/40 bg-emerald-800/10 px-4 py-2 text-sm font-medium text-emerald-300 transition-all hover:border-emerald-700/60 hover:bg-emerald-800/20 disabled:cursor-not-allowed disabled:opacity-40"
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
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-slate-400 transition-all hover:bg-slate-800 disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {action === "override" && (
            <div className="animate-fade-in space-y-3">
              <p className="text-xs text-slate-500">
                Your edited section replaces the Scribe&apos;s draft and is recorded as{" "}
                <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-slate-300">
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
                      className="h-72 w-full rounded-lg border border-border bg-slate-900/80 p-3 font-mono text-xs leading-relaxed text-slate-200 placeholder-slate-600 transition-colors focus:border-emerald-500/60 focus:outline-none focus:ring-1 focus:ring-emerald-500/30"
                      value={editContent}
                      onChange={(e) => setEditContent(e.target.value)}
                      autoFocus
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="block text-xs font-medium uppercase tracking-wider text-slate-400">
                      Live preview
                    </label>
                    <div className="h-72 overflow-y-auto rounded-lg border border-border bg-background p-3">
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
                <div className="h-96 overflow-y-auto rounded-lg border border-border bg-background p-4">
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
                  className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-300 transition-all hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40"
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
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-slate-400 transition-all hover:bg-slate-800 disabled:opacity-40"
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
        "rounded-t-lg px-3.5 py-2 text-xs font-medium transition-colors",
        active
          ? "bg-emerald-500/10 text-emerald-300 ring-1 ring-inset ring-emerald-500/20"
          : "text-slate-500 hover:text-slate-300",
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
          ? "bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-500/30"
          : "text-slate-500 hover:text-slate-300",
      )}
    >
      {children}
    </button>
  );
}

function DiffPane({ ops }: { ops: DiffOp[] }) {
  if (ops.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-background px-4 py-6 text-center text-xs text-slate-500">
        Nothing to compare yet.
      </p>
    );
  }
  return (
    <div className="h-96 overflow-y-auto rounded-lg border border-border bg-background font-mono text-[11px] leading-relaxed">
      {ops.map((op, i) => {
        if (op.type === "keep") {
          return (
            <div key={i} className="flex gap-3 border-b border-border px-3 py-0.5 text-slate-500">
              <span className="w-3 shrink-0 text-slate-700"> </span>
              <span className="whitespace-pre-wrap break-words">{op.edited || " "}</span>
            </div>
          );
        }
        if (op.type === "add") {
          return (
            <div
              key={i}
              className="flex gap-3 border-b border-border bg-emerald-500/10 px-3 py-0.5 text-emerald-300"
            >
              <span className="w-3 shrink-0 select-none text-emerald-500">+</span>
              <span className="whitespace-pre-wrap break-words">{op.edited || " "}</span>
            </div>
          );
        }
        return (
          <div
            key={i}
            className="flex gap-3 border-b border-border bg-red-500/10 px-3 py-0.5 text-red-300/90"
          >
            <span className="w-3 shrink-0 select-none text-red-500">−</span>
            <span className="whitespace-pre-wrap break-words line-through decoration-red-500/40">
              {op.original || " "}
            </span>
          </div>
        );
      })}
    </div>
  );
}
