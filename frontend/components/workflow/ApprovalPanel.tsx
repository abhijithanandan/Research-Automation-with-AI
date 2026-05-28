"use client";

import { useState } from "react";

export interface OverridePayload {
  artifact_kind: "matrix" | "summary" | "section" | "figure" | "code" | "log";
  label: string;
  content: string;
  mime_type: string;
}

interface ApprovalPanelProps {
  summary: string;
  busy: boolean;
  onApprove: () => void;
  onReject: (feedback: string) => void;
  onOverride: (payload: OverridePayload) => void;
}

type Action = "idle" | "reject" | "override";

export function ApprovalPanel({ summary, busy, onApprove, onReject, onOverride }: ApprovalPanelProps) {
  const [action, setAction] = useState<Action>("idle");
  const [feedback, setFeedback] = useState("");
  const [overrideContent, setOverrideContent] = useState("");
  const [overrideLabel, setOverrideLabel] = useState("");
  const [overrideKind, setOverrideKind] = useState<OverridePayload["artifact_kind"]>("log");

  function handleRejectSubmit() {
    if (!feedback.trim()) return;
    onReject(feedback.trim());
  }

  function handleOverrideSubmit() {
    if (!overrideContent.trim() || !overrideLabel.trim()) return;
    onOverride({
      artifact_kind: overrideKind,
      label: overrideLabel.trim(),
      content: overrideContent.trim(),
      mime_type: overrideKind === "section" || overrideKind === "summary" ? "text/markdown" : "text/plain",
    });
  }

  return (
    // Borderless review gate — left emerald rule + glow marks the
    // awaiting-review state without a boxed card (matches Synthesis/Section).
    <div className="animate-fade-in glow-emerald border-l-2 border-primary-dim pl-5">
      <div className="flex items-center gap-2.5 pb-4">
        <span className="animate-pulse-dot flex h-2 w-2 rounded-full bg-primary" />
        <p className="font-display text-base font-bold text-primary">Awaiting your approval</p>
      </div>

      <div className="space-y-4">
        <p className="text-sm text-muted">{summary}</p>

        {action === "idle" && (
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
                    <path d="M3 8l4 4 6-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                  Approve &amp; proceed
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
                <path d="M8 3v5M8 10v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
              Reject &amp; regenerate
            </button>

            <button
              type="button"
              onClick={() => setAction("override")}
              disabled={busy}
              className="flex items-center gap-2 rounded-lg bg-surface-elevated/50 border border-border px-4 py-2 text-sm font-medium text-foreground transition-all hover:bg-surface-elevated  disabled:cursor-not-allowed disabled:opacity-40"
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                <path d="M11 2l3 3-8 8H3v-3l8-8z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Manual override
            </button>
          </div>
        )}

        {action === "reject" && (
          <div className="space-y-3 animate-fade-in">
            <label className="block text-xs font-medium text-muted uppercase tracking-wider">
              Feedback for regeneration
            </label>
            <textarea
              className="w-full rounded-lg border border-border bg-surface-elevated p-3 text-sm text-foreground placeholder-muted-foreground/50 focus:border-primary-dim/60 focus:outline-none focus:ring-1 focus:ring-primary-dim/30 transition-colors"
              rows={3}
              placeholder="Describe what the Librarian should change on the next run…"
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
                onClick={() => { setAction("idle"); setFeedback(""); }}
                disabled={busy}
                className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted transition-all hover:bg-surface-elevated disabled:opacity-40"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {action === "override" && (
          <div className="space-y-3 animate-fade-in">
            <p className="text-xs text-muted">
              Your content replaces the agent output and is recorded as{" "}
              <code className="rounded bg-surface-elevated px-1.5 py-0.5 font-mono text-foreground">produced_by: human</code>{" "}
              in the audit log.
            </p>
            <div className="flex gap-3">
              <div className="flex-1">
                <label className="block text-xs font-medium text-muted uppercase tracking-wider mb-1.5">
                  Label
                </label>
                <input
                  type="text"
                  className="w-full rounded-lg border border-border bg-surface-elevated p-2.5 text-sm text-foreground placeholder-muted-foreground/50 focus:border-primary/60 focus:outline-none focus:ring-1 focus:ring-primary/30 transition-colors"
                  placeholder="e.g. discovery-pool"
                  value={overrideLabel}
                  onChange={(e) => setOverrideLabel(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted uppercase tracking-wider mb-1.5">
                  Kind
                </label>
                <select
                  className="rounded-lg border border-border bg-surface-elevated p-2.5 text-sm text-foreground focus:border-primary/60 focus:outline-none transition-colors"
                  value={overrideKind}
                  onChange={(e) => setOverrideKind(e.target.value as OverridePayload["artifact_kind"])}
                >
                  <option value="log">log</option>
                  <option value="summary">summary</option>
                  <option value="matrix">matrix</option>
                  <option value="section">section</option>
                  <option value="code">code</option>
                  <option value="figure">figure</option>
                </select>
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-muted uppercase tracking-wider mb-1.5">
                Content
              </label>
              <textarea
                className="w-full rounded-lg border border-border bg-surface-elevated p-3 font-mono text-sm text-foreground placeholder-muted-foreground/50 focus:border-primary/60 focus:outline-none focus:ring-1 focus:ring-primary/30 transition-colors"
                rows={6}
                placeholder="Paste or type the replacement content…"
                value={overrideContent}
                onChange={(e) => setOverrideContent(e.target.value)}
                autoFocus
              />
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={handleOverrideSubmit}
                disabled={busy || !overrideContent.trim() || !overrideLabel.trim()}
                className="rounded-lg border border-primary/40 bg-primary/10 px-4 py-2 text-sm font-medium text-primary transition-all hover:bg-primary/15 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? "Working…" : "Submit override"}
              </button>
              <button
                type="button"
                onClick={() => { setAction("idle"); setOverrideContent(""); setOverrideLabel(""); }}
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
  );
}
