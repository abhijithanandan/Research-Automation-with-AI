import type { Phase } from "@/lib/types";
import { cn } from "@/lib/utils";

// MVP per BRD §8 ships Phases 1, 2, 4 — Phase 3 (Analyst) is v0.2 and is
// intentionally absent from the tracker so users don't see a never-completing
// "Analysis" step between Synthesis and Drafting.
const PHASES: { key: Phase; label: string; icon: string }[] = [
  { key: "discovery", label: "Discovery",  icon: "01" },
  { key: "synthesis", label: "Synthesis",  icon: "02" },
  { key: "drafting",  label: "Drafting",   icon: "03" },
  { key: "done",      label: "Done",       icon: "✓" },
];

export function PhaseTracker({ current }: { current: Phase }) {
  const currentIdx = PHASES.findIndex((p) => p.key === current);

  return (
    <div className="flex items-center gap-0">
      {PHASES.map((p, i) => {
        const state = i < currentIdx ? "done" : i === currentIdx ? "active" : "pending";
        const isLast = i === PHASES.length - 1;

        return (
          <div key={p.key} className="flex items-center">
            {/* Step */}
            <div className="flex items-center gap-2">
              <div
                className={cn(
                  "flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold transition-all duration-300",
                  state === "done"    && "bg-emerald-800/30 text-emerald-300/70 ring-1 ring-emerald-700/40",
                  state === "active"  && "bg-emerald-500/20 text-emerald-300 ring-1 ring-emerald-500/60 shadow-[0_0_12px_oklch(72%_0.20_155_/_0.35)]",
                  state === "pending" && "bg-background text-slate-600 ring-1 ring-border",
                )}
              >
                {state === "done" ? "✓" : p.icon}
              </div>
              <span
                className={cn(
                  "text-xs font-medium transition-colors duration-300",
                  state === "done"    && "text-emerald-400/70",
                  state === "active"  && "text-emerald-300",
                  state === "pending" && "text-slate-600",
                )}
              >
                {p.label}
              </span>
            </div>

            {/* Connector */}
            {!isLast && (
              <div className="mx-3 flex-1">
                <div
                  className={cn(
                    "h-px w-8 transition-all duration-500",
                    i < currentIdx ? "bg-emerald-500/40" : "bg-border",
                  )}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
