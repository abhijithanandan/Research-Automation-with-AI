"use client";

import { useEffect, useState } from "react";

import type { AnalystProposal, AnalystResult, StaticScanResult } from "@/lib/types";
import { cn } from "@/lib/utils";

interface CommonProps {
  busy?: boolean;
}

interface CodeReviewProps extends CommonProps {
  proposal: AnalystProposal;
  onApprove: (overrideCode?: string) => Promise<void>;
  onReject: (feedback: string) => Promise<void>;
}

interface ResultsReviewProps extends CommonProps {
  result: AnalystResult;
  onApprove: () => Promise<void>;
  onReject: (feedback: string) => Promise<void>;
}

interface Props extends CommonProps {
  /** Discriminator from the `approval.required` WS event. */
  gate: "code" | "results";
  proposal?: AnalystProposal | null;
  result?: AnalystResult | null;
  /** Approve handlers, set conditionally on `gate`. */
  onApproveCode?: (overrideCode?: string) => Promise<void>;
  onApproveResults?: () => Promise<void>;
  /** Reject handlers, set conditionally on `gate`. */
  onRejectCode?: (feedback: string) => Promise<void>;
  onRejectResults?: (feedback: string) => Promise<void>;
}

const _LANG_HEADER = (
  <span className="text-[10px] font-mono uppercase tracking-wider text-emerald-400">
    python
  </span>
);

function ScanSummary({ scan }: { scan: StaticScanResult }) {
  if (scan.error) {
    return (
      <div className="rounded-md border border-red-700 bg-red-950/40 px-3 py-2 text-sm text-red-300">
        <strong>Syntax error blocks execution:</strong> {scan.error}
      </div>
    );
  }
  if (!scan.ok) {
    return (
      <div className="rounded-md border border-red-700 bg-red-950/40 px-3 py-2 text-sm text-red-300">
        <strong>Denied imports:</strong> {scan.denied.join(", ")} — the
        sandbox will refuse to run this. Edit the code below before approving.
      </div>
    );
  }
  if (scan.unknown.length > 0) {
    return (
      <div className="rounded-md border border-amber-700 bg-amber-950/30 px-3 py-2 text-sm text-amber-300">
        <strong>Unknown imports:</strong> {scan.unknown.join(", ")} — these
        modules are not pre-installed in the sandbox image. The code will
        fail at import time unless you override.
      </div>
    );
  }
  return (
    <div className="rounded-md border border-emerald-800 bg-emerald-950/30 px-3 py-2 text-sm text-emerald-300">
      Static scan passed. The code uses only the pre-installed analysis
      libraries (pandas, numpy, matplotlib, scipy, scikit-learn).
    </div>
  );
}

function CodeReview({ proposal, onApprove, onReject, busy }: CodeReviewProps) {
  const [editing, setEditing] = useState(false);
  const [override, setOverride] = useState<string>(proposal.code.content);
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);

  // Reset the override buffer when a fresh proposal arrives (i.e. after a
  // reject + regenerate). Without this the editor would still hold the
  // user's previous edits over the new LLM output.
  useEffect(() => {
    setOverride(proposal.code.content);
    setEditing(false);
  }, [proposal.code.id, proposal.code.content]);

  const canApprove = proposal.scan.ok && !proposal.scan.error;

  return (
    <section className="space-y-4" aria-label="Analyst code review">
      <header>
        <h3 className="font-display text-lg font-bold">Analyst — proposed code</h3>
        <p className="text-xs text-muted-foreground">
          Review before execution (BRD §10). The sandbox is hardened
          (--network=none, --read-only, --cap-drop=ALL, 60s timeout) but
          the user is the final gate.
        </p>
      </header>

      <ScanSummary scan={proposal.scan} />

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          {_LANG_HEADER}
          {!editing && (
            <button
              type="button"
              onClick={() => setEditing(true)}
              disabled={busy}
              className="text-xs text-emerald-400 hover:text-emerald-300"
            >
              Edit & override
            </button>
          )}
        </div>
        {editing ? (
          <textarea
            value={override}
            onChange={(e) => setOverride(e.target.value)}
            disabled={busy}
            spellCheck={false}
            rows={20}
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs text-slate-200 focus:border-emerald-500 focus:outline-none"
          />
        ) : (
          <pre className="max-h-[400px] overflow-auto rounded-md border border-slate-800 bg-slate-950 px-3 py-2 font-mono text-xs text-slate-200">
            {proposal.code.content}
          </pre>
        )}
      </div>

      {proposal.methods_narrative && (
        <div className="rounded-md border border-slate-800 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
          <p className="mb-1 text-[10px] font-mono uppercase tracking-wider text-slate-500">
            Methods narrative (drops into the manuscript)
          </p>
          <p className="italic">{proposal.methods_narrative}</p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void onApprove(editing ? override : undefined)}
          disabled={busy || (!editing && !canApprove)}
          className={cn(
            "rounded-md px-4 py-2 text-sm font-medium",
            "bg-emerald-700 text-white hover:bg-emerald-600",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          {editing ? "Approve override" : "Approve & run sandbox"}
        </button>
        <button
          type="button"
          onClick={() => setShowFeedback(true)}
          disabled={busy}
          className="rounded-md border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:border-amber-700 hover:text-amber-300"
        >
          Reject & regenerate
        </button>
      </div>

      {showFeedback && (
        <div className="mt-2 space-y-2 rounded-md border border-amber-800 bg-amber-950/20 p-3">
          <label className="block text-xs text-amber-300">
            What should the Analyst do differently?
          </label>
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            rows={3}
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200 focus:border-amber-500 focus:outline-none"
            placeholder="e.g. use seaborn for the histogram; group by region instead of country"
          />
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => {
                void onReject(feedback.trim());
                setShowFeedback(false);
                setFeedback("");
              }}
              disabled={busy || !feedback.trim()}
              className="rounded-md bg-amber-700 px-3 py-1 text-sm text-white hover:bg-amber-600 disabled:opacity-50"
            >
              Send revision request
            </button>
            <button
              type="button"
              onClick={() => setShowFeedback(false)}
              className="rounded-md border border-slate-700 px-3 py-1 text-sm text-slate-400"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function ResultsReview({ result, onApprove, onReject, busy }: ResultsReviewProps) {
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);

  const exitOk = result.exit_code === 0;
  const figures_b64 = result.figures_b64 ?? [];

  return (
    <section className="space-y-4" aria-label="Analyst results review">
      <header>
        <h3 className="font-display text-lg font-bold">Analyst — execution results</h3>
        <p className="text-xs text-muted-foreground">
          {result.duration_ms} ms · exit {result.exit_code}
          {result.timed_out && " · timed out"}
          {result.oomed && " · OOM-killed"}
        </p>
      </header>

      {!exitOk && (
        <div className="rounded-md border border-red-700 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          The script exited with status {result.exit_code}. Review the
          stderr panel and reject + regenerate if needed.
        </div>
      )}

      {figures_b64.length > 0 && (
        <div className="space-y-2">
          <p className="text-[10px] font-mono uppercase tracking-wider text-slate-500">
            Figures ({figures_b64.length})
          </p>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {figures_b64.map((b64, i) => (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={i}
                alt={`Figure ${i + 1}`}
                src={`data:image/png;base64,${b64}`}
                className="rounded-md border border-slate-800 bg-slate-900"
              />
            ))}
          </div>
        </div>
      )}

      {result.stdout && (
        <details className="rounded-md border border-slate-800 bg-slate-950">
          <summary className="cursor-pointer px-3 py-2 text-xs font-mono uppercase text-slate-400">
            stdout ({result.stdout.length} chars)
          </summary>
          <pre className="max-h-64 overflow-auto px-3 py-2 font-mono text-xs text-slate-200">
            {result.stdout}
          </pre>
        </details>
      )}
      {result.stderr && (
        <details
          open={!exitOk}
          className="rounded-md border border-slate-800 bg-slate-950"
        >
          <summary className="cursor-pointer px-3 py-2 text-xs font-mono uppercase text-red-400">
            stderr ({result.stderr.length} chars)
          </summary>
          <pre className="max-h-64 overflow-auto px-3 py-2 font-mono text-xs text-red-200">
            {result.stderr}
          </pre>
        </details>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void onApprove()}
          disabled={busy}
          className="rounded-md bg-emerald-700 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          Approve & continue to drafting
        </button>
        <button
          type="button"
          onClick={() => setShowFeedback(true)}
          disabled={busy}
          className="rounded-md border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:border-amber-700 hover:text-amber-300"
        >
          Reject & regenerate
        </button>
      </div>

      {showFeedback && (
        <div className="mt-2 space-y-2 rounded-md border border-amber-800 bg-amber-950/20 p-3">
          <label className="block text-xs text-amber-300">
            What needs to change?
          </label>
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            rows={3}
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200 focus:border-amber-500 focus:outline-none"
          />
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => {
                void onReject(feedback.trim());
                setShowFeedback(false);
                setFeedback("");
              }}
              disabled={busy || !feedback.trim()}
              className="rounded-md bg-amber-700 px-3 py-1 text-sm text-white hover:bg-amber-600 disabled:opacity-50"
            >
              Send revision request
            </button>
            <button
              type="button"
              onClick={() => setShowFeedback(false)}
              className="rounded-md border border-slate-700 px-3 py-1 text-sm text-slate-400"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

/** Top-level Phase-3 review surface. Routes between the code-review and
 *  results-review sub-views based on the `gate` discriminator from the
 *  `approval.required` WS event. */
export function AnalysisReview({
  gate,
  proposal,
  result,
  onApproveCode,
  onRejectCode,
  onApproveResults,
  onRejectResults,
  busy,
}: Props) {
  if (gate === "code" && proposal && onApproveCode && onRejectCode) {
    return (
      <CodeReview
        proposal={proposal}
        onApprove={onApproveCode}
        onReject={onRejectCode}
        busy={busy}
      />
    );
  }
  if (gate === "results" && result && onApproveResults && onRejectResults) {
    return (
      <ResultsReview
        result={result}
        onApprove={onApproveResults}
        onReject={onRejectResults}
        busy={busy}
      />
    );
  }
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/40 px-3 py-4 text-sm text-slate-400">
      Waiting for the Analyst…
    </div>
  );
}
