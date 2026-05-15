import type { Phase } from "@/lib/types";
import { cn } from "@/lib/utils";

const PHASES: { key: Phase; label: string }[] = [
  { key: "discovery", label: "Discovery" },
  { key: "synthesis", label: "Synthesis" },
  { key: "analysis", label: "Analysis" },
  { key: "drafting", label: "Drafting" },
  { key: "done", label: "Done" },
];

export function PhaseTracker({ current }: { current: Phase }) {
  const currentIdx = PHASES.findIndex((p) => p.key === current);
  return (
    <ol className="flex items-center gap-2 text-sm">
      {PHASES.map((p, i) => {
        const state =
          i < currentIdx ? "done" : i === currentIdx ? "active" : "pending";
        return (
          <li
            key={p.key}
            className={cn(
              "rounded-full border px-3 py-1",
              state === "done" && "border-green-500 bg-green-50 text-green-700",
              state === "active" && "border-blue-500 bg-blue-50 text-blue-700",
              state === "pending" && "border-slate-200 text-slate-500",
            )}
          >
            {p.label}
          </li>
        );
      })}
    </ol>
  );
}
