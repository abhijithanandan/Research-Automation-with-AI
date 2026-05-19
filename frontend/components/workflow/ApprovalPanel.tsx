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
    <div className="animate-fade-in overflow-hidden rounded-xl border border-amber-500/20 bg-amber-500/5 glow-amber">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-amber-500/20 px-5 py-4">
        <span className="flex h-2 w-2 rounded-full bg-amber-400 animate-pulse-dot" />
        <p className="text-sm font-semibold text-amber-300">Awaiting your approval</p>
      </div>

      <div className="px-5 py-4 space-y-4">
        <p className="text-sm text-slate-400">{summary}</p>

        {action === "idle" && (
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              onClick={onApprove}
              disabled={busy}
              className="flex items-center gap-2 rounded-lg bg-emerald-500/10 border border-emerald-500/30 px-4 py-2 text-sm font-medium text-emerald-400 transition-all hover:bg-emerald-500/20 hover:border-emerald-500/50 hover:shadow-[0_0_12px_rgba(16,185,129,0.2)] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? (
                <>
                  <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-emerald-400/30 border-t-emerald-400" />
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
              className="flex items-center gap-2 rounded-lg bg-amber-500/10 border border-amber-500/30 px-4 py-2 text-sm font-medium text-amber-400 transition-all hover:bg-amber-500/20 hover:border-amber-500/50 disabled:cursor-not-allowed disabled:opacity-40"
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
              className="flex items-center gap-2 rounded-lg bg-slate-700/50 border border-slate-600/50 px-4 py-2 text-sm font-medium text-slate-300 transition-all hover:bg-slate-700 hover:border-slate-500 disabled:cursor-not-allowed disabled:opacity-40"
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
            <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider">
              Feedback for regeneration
            </label>
            <textarea
              className="w-full rounded-lg border border-slate-700 bg-slate-900/80 p-3 text-sm text-slate-200 placeholder-slate-600 focus:border-amber-500/50 focus:outline-none focus:ring-1 focus:ring-amber-500/30 transition-colors"
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
                className="rounded-lg bg-amber-500/10 border border-amber-500/30 px-4 py-2 text-sm font-medium text-amber-400 transition-all hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? "Working…" : "Submit & regenerate"}
              </button>
              <button
                type="button"
                onClick={() => { setAction("idle"); setFeedback(""); }}
                disabled={busy}
                className="rounded-lg border border-slate-700 px-4 py-2 text-sm font-medium text-slate-400 transition-all hover:bg-slate-800 disabled:opacity-40"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {action === "override" && (
          <div className="space-y-3 animate-fade-in">
            <p className="text-xs text-slate-500">
              Your content replaces the agent output and is recorded as{" "}
              <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-slate-300">produced_by: human</code>{" "}
              in the audit log.
            </p>
            <div className="flex gap-3">
              <div className="flex-1">
                <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">
                  Label
                </label>
                <input
                  type="text"
                  className="w-full rounded-lg border border-slate-700 bg-slate-900/80 p-2.5 text-sm text-slate-200 placeholder-slate-600 focus:border-blue-500/50 focus:outline-none focus:ring-1 focus:ring-blue-500/30 transition-colors"
                  placeholder="e.g. discovery-pool"
                  value={overrideLabel}
                  onChange={(e) => setOverrideLabel(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">
                  Kind
                </label>
                <select
                  className="rounded-lg border border-slate-700 bg-slate-900/80 p-2.5 text-sm text-slate-200 focus:border-blue-500/50 focus:outline-none transition-colors"
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
              <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">
                Content
              </label>
              <textarea
                className="w-full rounded-lg border border-slate-700 bg-slate-900/80 p-3 font-mono text-sm text-slate-200 placeholder-slate-600 focus:border-blue-500/50 focus:outline-none focus:ring-1 focus:ring-blue-500/30 transition-colors"
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
                className="rounded-lg bg-blue-500/10 border border-blue-500/30 px-4 py-2 text-sm font-medium text-blue-400 transition-all hover:bg-blue-500/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? "Working…" : "Submit override"}
              </button>
              <button
                type="button"
                onClick={() => { setAction("idle"); setOverrideContent(""); setOverrideLabel(""); }}
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
  );
}
