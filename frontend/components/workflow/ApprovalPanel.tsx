"use client";

import { useState } from "react";

interface ApprovalPanelProps {
  summary: string;
  onApprove: () => void;
  onReject: (feedback: string) => void;
}

export function ApprovalPanel({ summary, onApprove, onReject }: ApprovalPanelProps) {
  const [feedback, setFeedback] = useState("");

  return (
    <div className="space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950">
      <p className="text-sm font-semibold text-amber-900 dark:text-amber-100">
        Awaiting your approval
      </p>
      <p className="text-sm text-amber-900/80 dark:text-amber-100/80">{summary}</p>
      <textarea
        className="w-full rounded border border-amber-300 bg-white p-2 text-sm dark:border-amber-700 dark:bg-slate-900"
        rows={3}
        placeholder="Optional feedback for regeneration..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
      />
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onApprove}
          className="rounded bg-green-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-700"
        >
          Approve &amp; proceed
        </button>
        <button
          type="button"
          onClick={() => onReject(feedback)}
          disabled={!feedback.trim()}
          className="rounded bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Reject &amp; regenerate
        </button>
      </div>
    </div>
  );
}
