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

  // Vertical stepper for the left nav rail. Each step is a numbered node with
  // a connecting rule running down to the next; the active step glows emerald.
  return (
    <ol className="flex flex-col">
      {PHASES.map((p, i) => {
        const state = i < currentIdx ? "done" : i === currentIdx ? "active" : "pending";
        const isLast = i === PHASES.length - 1;

        return (
          <li key={p.key} className="flex gap-3">
            {/* Node + connector column */}
            <div className="flex flex-col items-center">
              <div
                className={cn(
                  "flex h-7 w-7 shrink-0 items-center justify-center rounded-full font-mono text-[11px] font-semibold transition-all duration-300",
                  state === "done" && "bg-primary-dim-bg text-primary/70 ring-1 ring-primary-dim/40",
                  state === "active" && "bg-primary/15 text-primary ring-1 ring-primary/60 shadow-[0_0_12px_oklch(72%_0.20_155_/_0.35)]",
                  state === "pending" && "text-muted-foreground ring-1 ring-border",
                )}
              >
                {state === "done" ? "✓" : p.icon}
              </div>
              {!isLast && (
                <div
                  className={cn(
                    "my-1 w-px flex-1 transition-colors duration-500",
                    i < currentIdx ? "bg-primary/40" : "bg-border",
                  )}
                />
              )}
            </div>
            {/* Label */}
            <span
              className={cn(
                "pt-1 pb-5 text-sm font-medium transition-colors duration-300",
                state === "done" && "text-primary/70",
                state === "active" && "text-primary",
                state === "pending" && "text-muted-foreground",
              )}
            >
              {p.label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
