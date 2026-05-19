import type { Phase } from "@/lib/types";
import { cn } from "@/lib/utils";

const PHASES: { key: Phase; label: string; icon: string }[] = [
  { key: "discovery", label: "Discovery",  icon: "01" },
  { key: "synthesis", label: "Synthesis",  icon: "02" },
  { key: "analysis",  label: "Analysis",   icon: "03" },
  { key: "drafting",  label: "Drafting",   icon: "04" },
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
                  state === "done"    && "bg-emerald-500/20 text-emerald-400 ring-1 ring-emerald-500/40",
                  state === "active"  && "bg-blue-500/20 text-blue-400 ring-1 ring-blue-500/60 shadow-[0_0_12px_rgba(59,130,246,0.3)]",
                  state === "pending" && "bg-slate-800 text-slate-600 ring-1 ring-slate-700",
                )}
              >
                {state === "done" ? "✓" : p.icon}
              </div>
              <span
                className={cn(
                  "text-xs font-medium transition-colors duration-300",
                  state === "done"    && "text-emerald-400",
                  state === "active"  && "text-blue-400",
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
                    i < currentIdx ? "bg-emerald-500/40" : "bg-slate-700",
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
