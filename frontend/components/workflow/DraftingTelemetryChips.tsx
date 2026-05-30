"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { DraftingTelemetry } from "@/lib/types";

// ---------------------------------------------------------------------------
// Drafting Telemetry chip row (BRD NFR-6 / §9 success metrics)
// ---------------------------------------------------------------------------
//
// Surfaces the `drafting{}` block from GET /projects/{id}/usage as a compact
// chip strip. Re-fetches whenever `refreshKey` changes — page.tsx bumps it on
// each WS phase_4.section_ready event so the counters track live work.

interface DraftingTelemetryChipsProps {
  projectId: string;
  token: string;
  /** Bump this any time you want to force a refetch (WS section_ready, etc.). */
  refreshKey?: number;
}

function fmtMs(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function DraftingTelemetryChips({
  projectId,
  token,
  refreshKey = 0,
}: DraftingTelemetryChipsProps) {
  const [t, setT] = useState<DraftingTelemetry | null>(null);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    api.usage
      .get(projectId, token)
      .then((u) => {
        if (!cancelled) setT(u.drafting);
      })
      .catch(() => {
        // Non-blocking — telemetry is observational, never on the critical path.
        if (!cancelled) setT(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, token, refreshKey]);

  if (!t || t.sections_drafted === 0) return null;

  // Each chip is `<label> <value>` with monospaced numerics, terminal-clean.
  const chips: { label: string; value: string; tone?: "primary" | "warning" | "muted" }[] = [
    { label: "drafted", value: t.sections_drafted.toString(), tone: "primary" },
    { label: "avg/section", value: fmtMs(t.avg_section_ms), tone: "muted" },
    ...(t.regenerations > 0
      ? [{ label: "regens", value: t.regenerations.toString(), tone: "warning" as const }]
      : []),
    ...(t.overrides > 0
      ? [{ label: "overrides", value: t.overrides.toString(), tone: "muted" as const }]
      : []),
    ...(t.citation_corrections > 0
      ? [
          {
            label: "cite fixes",
            value: t.citation_corrections.toString(),
            tone: "warning" as const,
          },
        ]
      : []),
  ];

  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-l-2 border-primary-dim/60 pl-3"
      aria-label="Phase 4 telemetry"
    >
      {chips.map(({ label, value, tone }) => (
        <span
          key={label}
          className="font-mono text-[11px] uppercase tracking-[0.12em] text-muted"
        >
          {label}
          <span
            className={
              tone === "primary"
                ? "ml-1.5 text-primary"
                : tone === "warning"
                  ? "ml-1.5 text-warning"
                  : "ml-1.5 text-foreground"
            }
          >
            {value}
          </span>
        </span>
      ))}
    </div>
  );
}
