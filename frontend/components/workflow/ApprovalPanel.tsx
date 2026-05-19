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

export function ApprovalPanel({
  summary,
  busy,
  onApprove,
  onReject,
  onOverride,
}: ApprovalPanelProps) {
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
    <div className="space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950">
      <p className="text-sm font-semibold text-amber-900 dark:text-amber-100">
        Awaiting your approval
      </p>
      <p className="text-sm text-amber-900/80 dark:text-amber-100/80">{summary}</p>

      {action === "idle" && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onApprove}
            disabled={busy}
            className="rounded bg-green-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "Working…" : "Approve & proceed"}
          </button>
          <button
            type="button"
            onClick={() => setAction("reject")}
            disabled={busy}
            className="rounded bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Reject & regenerate
          </button>
          <button
            type="button"
            onClick={() => setAction("override")}
            disabled={busy}
            className="rounded bg-slate-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Manual override
          </button>
        </div>
      )}

      {action === "reject" && (
        <div className="space-y-2">
          <label className="block text-xs font-medium text-amber-900 dark:text-amber-100">
            Feedback for regeneration
          </label>
          <textarea
            className="w-full rounded border border-amber-300 bg-white p-2 text-sm dark:border-amber-700 dark:bg-slate-900"
            rows={3}
            placeholder="Describe what the agent should change…"
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            autoFocus
          />
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleRejectSubmit}
              disabled={busy || !feedback.trim()}
              className="rounded bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? "Working…" : "Submit & regenerate"}
            </button>
            <button
              type="button"
              onClick={() => { setAction("idle"); setFeedback(""); }}
              disabled={busy}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {action === "override" && (
        <div className="space-y-2">
          <p className="text-xs text-amber-800 dark:text-amber-200">
            Your content replaces the agent output and is recorded as{" "}
            <code className="rounded bg-amber-100 px-1 dark:bg-amber-900">produced_by: human</code>{" "}
            in the audit log.
          </p>
          <div className="flex gap-2">
            <div className="flex-1">
              <label className="block text-xs font-medium text-amber-900 dark:text-amber-100">
                Label
              </label>
              <input
                type="text"
                className="mt-1 w-full rounded border border-amber-300 bg-white p-2 text-sm dark:border-amber-700 dark:bg-slate-900"
                placeholder="e.g. discovery-pool"
                value={overrideLabel}
                onChange={(e) => setOverrideLabel(e.target.value)}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-amber-900 dark:text-amber-100">
                Kind
              </label>
              <select
                className="mt-1 rounded border border-amber-300 bg-white p-2 text-sm dark:border-amber-700 dark:bg-slate-900"
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
          <label className="block text-xs font-medium text-amber-900 dark:text-amber-100">
            Content
          </label>
          <textarea
            className="w-full rounded border border-amber-300 bg-white p-2 font-mono text-sm dark:border-amber-700 dark:bg-slate-900"
            rows={6}
            placeholder="Paste or type the replacement content…"
            value={overrideContent}
            onChange={(e) => setOverrideContent(e.target.value)}
            autoFocus
          />
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleOverrideSubmit}
              disabled={busy || !overrideContent.trim() || !overrideLabel.trim()}
              className="rounded bg-slate-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? "Working…" : "Submit override"}
            </button>
            <button
              type="button"
              onClick={() => { setAction("idle"); setOverrideContent(""); setOverrideLabel(""); }}
              disabled={busy}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
