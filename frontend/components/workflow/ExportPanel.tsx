"use client";

import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { ExportFormat } from "@/lib/types";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Export Pack picker (BRD FR-3.5)
// ---------------------------------------------------------------------------
//
// The Done screen surfaces four download options once Phase 4 produces a
// manuscript artifact. The legacy "Download .md" button stays as the
// recommended path; this panel adds bibtex, package (ZIP), and bundle.

const FORMAT_LABELS: Record<ExportFormat, { name: string; subtitle: string }> = {
  markdown: { name: "Markdown", subtitle: ".md — just the assembled manuscript" },
  bibtex: { name: "BibTeX", subtitle: ".bib — approved-pool references only" },
  package: {
    name: "Package (ZIP)",
    subtitle: "manuscript + references + disclosure + audit appendix",
  },
  bundle: {
    name: "Bundle (single .md)",
    subtitle: "everything in one combined markdown file",
  },
};

const FORMAT_ORDER: ExportFormat[] = ["markdown", "bibtex", "package", "bundle"];

interface ExportPanelProps {
  projectId: string;
  token: string;
}

export function ExportPanel({ projectId, token }: ExportPanelProps) {
  const [format, setFormat] = useState<ExportFormat>("package");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDownload() {
    setError(null);
    setBusy(true);
    try {
      const { blob, filename } = await api.exports.download(projectId, format, token);
      // Save via an <a download> dance — same pattern the legacy .md button uses.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      if (err instanceof ApiError && err.code === "manuscript_not_ready") {
        setError("Manuscript not ready yet — finish Phase 4 first.");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Download failed.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3" aria-label="Export pack">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        Export pack
      </p>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {FORMAT_ORDER.map((f) => {
          const active = format === f;
          return (
            <button
              key={f}
              type="button"
              onClick={() => setFormat(f)}
              className={cn(
                "rounded-md border px-3 py-2.5 text-left transition-all",
                active
                  ? "border-primary/60 bg-primary/10 text-foreground ring-1 ring-inset ring-primary/30"
                  : "border-border bg-surface-elevated/40 text-muted hover:border-border hover:bg-surface-elevated",
              )}
              aria-pressed={active}
            >
              <p className="text-sm font-semibold text-foreground">
                {FORMAT_LABELS[f].name}
              </p>
              <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                {FORMAT_LABELS[f].subtitle}
              </p>
            </button>
          );
        })}
      </div>

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => void handleDownload()}
          disabled={busy}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground transition-all duration-200 hover:bg-primary-hover hover:shadow-[0_0_20px_oklch(72%_0.20_155_/_0.35)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? (
            <>
              <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary/30 border-t-primary-foreground" />
              Preparing…
            </>
          ) : (
            <>
              <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
                <path
                  d="M8 2v9m0 0l-3-3m3 3l3-3M3 13h10"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Download {FORMAT_LABELS[format].name}
            </>
          )}
        </button>
        {error && (
          <p className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}
      </div>
    </section>
  );
}
