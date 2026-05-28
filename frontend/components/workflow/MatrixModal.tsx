"use client";

// Full-screen comparison-matrix modal.
//
// Mounted into document.body via React Portal so the matrix can break out of
// the page's max-width cap and use the full monitor width. The legacy in-card
// matrix view squeezed a 6-column table into a ~672px-wide container, forcing
// horizontal scroll inside vertical scroll — exactly the "nested scrollbars"
// trap the mandate (2026-05-27) called out.

import { useEffect } from "react";
import { createPortal } from "react-dom";

import { MatrixTable, type MatrixModel } from "@/components/workflow/SynthesisReview";
import type { Paper } from "@/lib/types";

interface MatrixModalProps {
  /** Parsed matrix JSON. `null` is allowed (caller guards on render). */
  matrix: MatrixModel | null;
  /** Citation-key → Paper lookup the matrix uses to render human-readable
   * titles instead of opaque BibTeX keys. */
  paperByKey: Map<string, Paper>;
  open: boolean;
  onClose: () => void;
}

export function MatrixModal({ matrix, paperByKey, open, onClose }: MatrixModalProps) {
  // ESC-to-close + body-scroll lock while the modal is open. Effect is no-op
  // when `open` is false so the listeners aren't permanently attached.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);

  // SSR guard — `document` doesn't exist during Next 14's server pass.
  if (!open || typeof document === "undefined") return null;
  if (!matrix) return null;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Comparison matrix — fullscreen view"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        // Stop bubbling so clicking inside the card doesn't close the modal.
        onClick={(e) => e.stopPropagation()}
        // A single hairline + emerald glow defines the floating surface against
        // the backdrop — functional delineation, not a content box.
        className="glow-emerald-strong flex h-[95vh] w-[95vw] flex-col overflow-hidden rounded-xl border border-border bg-surface-elevated"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <div className="flex items-baseline gap-3">
            <h2 className="font-display text-base font-bold text-foreground">
              Comparison matrix
            </h2>
            <span className="font-mono text-[11px] uppercase tracking-[0.15em] text-primary/70">
              {matrix.rows.length} paper{matrix.rows.length !== 1 ? "s" : ""}
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close fullscreen matrix (ESC)"
            autoFocus
            className="flex h-8 w-8 items-center justify-center rounded-md text-lg text-muted transition-all duration-200 hover:bg-primary/10 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
          >
            ×
          </button>
        </div>

        {/* Scroll region — the ONLY scroll container in the modal. Lets the
            table grow to whatever width the columns demand without nesting
            scrolls inside the page. */}
        <div className="flex-1 overflow-auto px-6 py-5">
          <MatrixTable rows={matrix.rows} paperByKey={paperByKey} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
