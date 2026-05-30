"use client";

import { useEffect, useMemo, useState } from "react";

import { diffLines, diffStats, type DiffOp } from "@/components/workflow/diffLines";
import { Markdown } from "@/components/workflow/Markdown";
import { ApiError, api } from "@/lib/api";
import type { Artifact, CitationPanel, SectionName } from "@/lib/types";
import { cn } from "@/lib/utils";

// Dev token — matches the pattern used in page.tsx for the rest of the app.
const DEV_TOKEN = process.env.NEXT_PUBLIC_DEV_TOKEN ?? "dev-token";

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
  /** FR-1.5: map of `{bad_key: good_key}` applied to `content` before save. */
  citation_corrections?: Record<string, string>;
  /** Free-text rationale for the manual edit (audited as user.citation_correction). */
  override_reason?: string | null;
}

interface SectionReviewProps {
  section: Artifact | null;
  /** Current section name from the WS `approval.required` event. */
  currentSection: SectionName | null;
  /** Project id — needed to fetch citations from /drafting/citations. */
  projectId: string;
  loading: boolean;
  busy: boolean;
  /** Approve may be called with `{force_unresolved, override_reason}` (FR-1.5). */
  onApprove: (opts?: {
    force_unresolved?: boolean;
    override_reason?: string | null;
  }) => Promise<void>;
  onReject: (feedback: string) => void;
  onOverride: (payload: SectionOverridePayload) => void;
}

type Tab = "preview" | "source" | "citations" | "diff";
type Action = "idle" | "reject" | "override";
type EditView = "edit" | "diff" | "preview";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SectionReview({
  section,
  currentSection,
  projectId,
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
  // Citation Manager v1 (FR-1.5) — source of truth from /drafting/citations.
  const [citations, setCitations] = useState<CitationPanel | null>(null);
  // FR-1.4 diff view at the section gate — prior approved/rejected draft of
  // the SAME section, surfaced when one exists (after reject→redraft).
  const [prevContent, setPrevContent] = useState<string | null>(null);
  // Force-approve flow: when the backend returns 409 unresolved_citations.
  const [forceState, setForceState] = useState<
    { keys: string[]; reason: string } | null
  >(null);
  // Override-mode citation corrections: bad_key → approved_key.
  const [corrections, setCorrections] = useState<Record<string, string>>({});
  const [overrideReason, setOverrideReason] = useState("");

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

  // ── Fetch citations whenever the section artifact changes ─────────────
  // section.id changes on reject→redraft, so this re-runs and reflects the
  // new draft. Section name alone is not enough (it stays the same on retry).
  useEffect(() => {
    if (!section || !sectionName || !projectId) {
      setCitations(null);
      return;
    }
    let cancelled = false;
    api.drafting
      .citations(projectId, sectionName, DEV_TOKEN)
      .then((panel) => {
        if (!cancelled) setCitations(panel);
      })
      .catch(() => {
        // Non-blocking — the rest of the panel still works. The endpoint
        // returns 404 only if the project doesn't exist, which is a hard
        // error already surfaced elsewhere; for any other failure we just
        // fall back to "no citations detected" UX.
        if (!cancelled) setCitations(null);
      });
    return () => {
      cancelled = true;
    };
  }, [section, sectionName, projectId]);

  // ── Fetch the prior draft of THIS section, if one exists ─────────────
  // The diff tab is only useful after a reject→redraft cycle. We list every
  // section-kind artifact for the project, filter to the same label as the
  // current section, sort by created_at desc, and pick artifacts[1] (the one
  // immediately before the current one). Skip when no prior version exists.
  useEffect(() => {
    if (!section || !sectionName || !projectId) {
      setPrevContent(null);
      return;
    }
    let cancelled = false;
    api.artifacts
      .list(projectId, "section", DEV_TOKEN)
      .then((sections) => {
        const sameLabel = sections
          .filter((a) => a.label === sectionName && a.id !== section.id)
          .sort((a, b) => (b.created_at < a.created_at ? -1 : 1));
        if (!cancelled) {
          setPrevContent(sameLabel[0]?.content ?? null);
        }
      })
      .catch(() => {
        if (!cancelled) setPrevContent(null);
      });
    return () => {
      cancelled = true;
    };
  }, [section, sectionName, projectId]);

  const resolvedCitations = citations?.resolved ?? [];
  const unresolvedKeys = citations?.unresolved_keys ?? [];
  const citationCount = (citations?.cited_keys.length ?? 0);
  // Diff between the prior draft and the current draft — read-only, computed
  // only when the diff tab is the active tab so the LCS table is cheap.
  const prevVsCurrentOps = useMemo(
    () =>
      tab === "diff" && prevContent !== null
        ? diffLines(prevContent, sectionContent)
        : [],
    [tab, prevContent, sectionContent],
  );
  const prevVsCurrentStats = useMemo(
    () => diffStats(prevVsCurrentOps),
    [prevVsCurrentOps],
  );

  function startEditing() {
    setEditContent(sectionContent);
    setEditView("edit");
    // Pre-seed the corrections map with each unresolved key → "" so the
    // override panel surfaces the fix UI immediately.
    const seed: Record<string, string> = {};
    for (const k of unresolvedKeys) seed[k] = "";
    setCorrections(seed);
    setOverrideReason("");
    setAction("override");
  }

  function handleRejectSubmit() {
    if (!feedback.trim()) return;
    onReject(feedback.trim());
  }

  function handleOverrideSubmit() {
    if (!editContent.trim()) return;
    // Only forward corrections that have a non-empty replacement key — empty
    // ones mean the reviewer left them as unresolved (will block approve
    // unless force_unresolved is used afterward).
    const applied: Record<string, string> = {};
    for (const [bad, good] of Object.entries(corrections)) {
      if (good.trim()) applied[bad] = good.trim();
    }
    onOverride({
      artifact_kind: "section",
      label: section?.label ?? sectionName ?? "section",
      content: editContent.trim(),
      mime_type: "text/markdown",
      ...(Object.keys(applied).length > 0 ? { citation_corrections: applied } : {}),
      ...(overrideReason.trim() ? { override_reason: overrideReason.trim() } : {}),
    });
  }

  async function handleApprovePlain() {
    try {
      await onApprove();
    } catch (err) {
      // Backend 409 unresolved_citations — surface the keys and let the
      // reviewer either fix them via override or force-approve with a reason.
      if (err instanceof ApiError && err.code === "unresolved_citations") {
        const detail = (err.detail as { detail?: { keys?: string[] } } | undefined)?.detail;
        const keys = detail?.keys ?? unresolvedKeys;
        setForceState({ keys, reason: "" });
      }
    }
  }

  async function handleForceApproveSubmit() {
    if (!forceState) return;
    if (!forceState.reason.trim()) return;
    try {
      await onApprove({
        force_unresolved: true,
        override_reason: forceState.reason.trim(),
      });
      setForceState(null);
    } catch {
      // onApprove already routed errors to the error view; keep the dialog open.
    }
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

        {/* Unresolved-citation warning banner (keys cited but NOT in the approved pool) */}
        {unresolvedKeys.length > 0 && (
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
                  {unresolvedKeys.length} unresolved citation
                  {unresolvedKeys.length !== 1 ? "s" : ""} — approve is blocked
                </p>
                <p className="text-xs text-muted">
                  The draft cites{" "}
                  <code className="rounded bg-surface-elevated px-1 py-0.5 font-mono text-warning">
                    {unresolvedKeys.join(", ")}
                  </code>{" "}
                  but these keys are not in the approved pool. Edit &amp; override to map them
                  to valid keys, reject to regenerate, or force-approve with a written reason.
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
            Citations ({citationCount.toString()})
          </TabButton>
          {prevContent !== null && (
            <TabButton active={tab === "diff"} onClick={() => setTab("diff")}>
              Diff vs previous
            </TabButton>
          )}
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
            <div className="space-y-5">
              {citationCount === 0 && (
                <p className="text-xs text-muted">
                  No citations were detected in this section.
                </p>
              )}
              {resolvedCitations.length > 0 && (
                <div className="space-y-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                    From the approved pool ({resolvedCitations.length})
                  </p>
                  <div className="space-y-2">
                    {resolvedCitations.map((c) => (
                      <article
                        key={c.citation_key}
                        className="rounded-md border border-border bg-surface-elevated/40 p-3"
                      >
                        <div className="flex items-baseline justify-between gap-3">
                          <code className="font-mono text-[11px] text-primary">
                            [@{c.citation_key}]
                          </code>
                          {c.year !== null && (
                            <span className="font-mono text-[11px] text-muted">{c.year}</span>
                          )}
                        </div>
                        <p className="mt-1 text-sm font-medium text-foreground">{c.title}</p>
                        {c.authors.length > 0 && (
                          <p className="mt-0.5 text-xs text-muted">
                            {c.authors.slice(0, 6).join(", ")}
                            {c.authors.length > 6 ? ", et al." : ""}
                          </p>
                        )}
                        <div className="mt-2 flex items-center gap-3 text-[11px]">
                          <span className="rounded bg-surface-elevated px-1.5 py-0.5 font-mono uppercase tracking-wider text-muted-foreground">
                            {c.source}
                          </span>
                          {c.url && (
                            <a
                              href={c.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-primary hover:underline"
                            >
                              View source →
                            </a>
                          )}
                        </div>
                      </article>
                    ))}
                  </div>
                </div>
              )}
              {unresolvedKeys.length > 0 && (
                <div className="space-y-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-warning">
                    Unresolved ({unresolvedKeys.length}) — not in the approved pool
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {unresolvedKeys.map((k) => (
                      <code
                        key={k}
                        className="rounded-full border border-warning/40 bg-warning/10 px-2.5 py-0.5 font-mono text-[11px] text-warning"
                      >
                        [@{k}]
                      </code>
                    ))}
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    Edit &amp; override to remap each to a valid pool key, or force-approve
                    with a written reason.
                  </p>
                </div>
              )}
            </div>
          )}

          {tab === "diff" && prevContent !== null && (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3 border-b border-border/60 pb-2">
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
                  Previous draft (left) → current draft (right)
                </p>
                <div className="flex items-center gap-3 text-[11px]">
                  <span className="text-primary">+{prevVsCurrentStats.added}</span>
                  <span className="text-destructive">−{prevVsCurrentStats.removed}</span>
                </div>
              </div>
              <DiffPane ops={prevVsCurrentOps} />
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
          {forceState && (
            <div className="space-y-3 rounded-md border border-warning/40 bg-warning/5 p-4 animate-fade-in">
              <div className="space-y-1">
                <p className="text-xs font-semibold uppercase tracking-wider text-warning">
                  Approve is blocked — unresolved citations
                </p>
                <p className="text-xs text-muted">
                  The draft cites{" "}
                  <code className="rounded bg-surface-elevated px-1 py-0.5 font-mono text-warning">
                    {forceState.keys.map((k) => `[@${k}]`).join(", ")}
                  </code>
                  . Approving anyway is recorded as an audited override.
                </p>
              </div>
              <label className="block text-xs font-medium uppercase tracking-wider text-muted">
                Reason (required for force approve)
              </label>
              <textarea
                className="w-full rounded-lg border border-border bg-surface-elevated p-3 text-sm text-foreground placeholder-muted-foreground/50 focus:border-warning/60 focus:outline-none focus:ring-1 focus:ring-warning/30"
                rows={2}
                placeholder="e.g. intentional placeholders; will fix in revision pass…"
                value={forceState.reason}
                onChange={(e) =>
                  setForceState((s) => (s ? { ...s, reason: e.target.value } : s))
                }
                autoFocus
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => void handleForceApproveSubmit()}
                  disabled={busy || !forceState.reason.trim()}
                  className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-2 text-sm font-medium text-warning transition-all hover:bg-warning/15 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy ? "Working…" : "Force approve (audited)"}
                </button>
                <button
                  type="button"
                  onClick={() => setForceState(null)}
                  disabled={busy}
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted transition-all hover:bg-surface-elevated disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {action === "idle" && !forceState && (
            <>
              <p className="text-sm text-muted">
                Approve to advance to the next section, reject with feedback to regenerate,
                or edit the draft directly.
              </p>
              <div className="flex flex-wrap gap-3">
                <button
                  type="button"
                  onClick={() => void handleApprovePlain()}
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

              {Object.keys(corrections).length > 0 && (
                <div className="space-y-2 rounded-md border border-warning/30 bg-warning/5 p-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-warning">
                    Fix unresolved citation keys
                  </p>
                  <p className="text-[11px] text-muted">
                    For each bad key, type a valid approved-pool key. Each fix rewrites the
                    matching <code className="font-mono">[@bad]</code> marker in your edit and
                    is recorded as <code className="font-mono">user.citation_correction</code>.
                  </p>
                  <div className="space-y-1.5">
                    {Object.entries(corrections).map(([bad, good]) => (
                      <div key={bad} className="flex items-center gap-2">
                        <code className="w-40 shrink-0 rounded bg-surface-elevated px-2 py-1 font-mono text-[11px] text-warning">
                          [@{bad}]
                        </code>
                        <span className="text-[11px] text-muted-foreground">→</span>
                        <input
                          type="text"
                          value={good}
                          onChange={(e) =>
                            setCorrections((c) => ({ ...c, [bad]: e.target.value }))
                          }
                          placeholder="approved citation key (e.g. lecun2015)"
                          className="flex-1 rounded border border-border bg-surface-elevated px-2 py-1 font-mono text-[11px] text-foreground placeholder-muted-foreground/50 focus:border-primary/60 focus:outline-none focus:ring-1 focus:ring-primary/30"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="space-y-1">
                <label className="block text-[11px] font-medium uppercase tracking-wider text-muted">
                  Override reason (optional, audited)
                </label>
                <input
                  type="text"
                  value={overrideReason}
                  onChange={(e) => setOverrideReason(e.target.value)}
                  placeholder="e.g. fixed hallucinated keys; tightened claim"
                  className="w-full rounded border border-border bg-surface-elevated px-2 py-1.5 text-xs text-foreground placeholder-muted-foreground/50 focus:border-primary/60 focus:outline-none focus:ring-1 focus:ring-primary/30"
                />
              </div>

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
