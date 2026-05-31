"use client";

import { useCallback, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { Dataset } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  projectId: string;
  token: string;
  datasets: Dataset[];
  /** Called with the freshly-uploaded dataset after a 201 response so the
   *  parent can prepend it to its list without re-fetching. */
  onUploaded: (d: Dataset) => void;
  /** Called after a successful delete. */
  onDeleted: (id: string) => void;
  /** When true the uploader is read-only (e.g. analysis has started). */
  locked?: boolean;
}

const ALLOWED_EXT = [".csv", ".tsv", ".json", ".jsonl", ".parquet"];

function _formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
}

/** Phase-3 dataset uploader. Sits between project creation and the first
 * workflow start, and remains visible (read-only after analysis begins).
 *
 * Multipart upload, drag-and-drop, per-file status, and a list view that
 * surfaces the schema (columns + rowcount) the backend extracted at upload
 * time so the user can sanity-check the file before the Analyst runs.
 */
export function DatasetUploader({
  projectId,
  token,
  datasets,
  onUploaded,
  onDeleted,
  locked = false,
}: Props) {
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleFiles = useCallback(
    async (files: FileList | File[]) => {
      if (locked) return;
      const list = Array.from(files);
      if (list.length === 0) return;
      setBusy(true);
      setError(null);
      try {
        for (const file of list) {
          const ext = "." + (file.name.split(".").pop() ?? "").toLowerCase();
          if (!ALLOWED_EXT.includes(ext)) {
            throw new ApiError(
              "validation",
              422,
              "bad_extension",
              `File ${file.name} has unsupported extension ${ext}. Supported: ${ALLOWED_EXT.join(", ")}`,
              undefined,
            );
          }
          const created = await api.datasets.upload(projectId, file, token);
          onUploaded(created);
        }
      } catch (err) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError(err instanceof Error ? err.message : "Upload failed");
        }
      } finally {
        setBusy(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [locked, onUploaded, projectId, token],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      if (locked) return;
      setBusy(true);
      setError(null);
      try {
        await api.datasets.delete(projectId, id, token);
        onDeleted(id);
      } catch (err) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError(err instanceof Error ? err.message : "Delete failed");
        }
      } finally {
        setBusy(false);
      }
    },
    [locked, onDeleted, projectId, token],
  );

  return (
    <section className="space-y-3" aria-label="Project datasets">
      <header className="flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
          Datasets
          <span className="ml-2 rounded-full bg-slate-800 px-2 py-0.5 text-xs font-normal text-slate-400">
            Phase 3 input
          </span>
        </h3>
        {locked && (
          <span className="text-xs text-amber-400">
            Locked — analysis has started
          </span>
        )}
      </header>

      <div
        onDragOver={(e) => {
          e.preventDefault();
          if (!locked) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (!locked && e.dataTransfer?.files) {
            void handleFiles(e.dataTransfer.files);
          }
        }}
        className={cn(
          "rounded-md border-2 border-dashed p-6 text-center transition-colors",
          dragOver
            ? "border-emerald-400 bg-emerald-950/30"
            : "border-slate-700 hover:border-slate-500",
          locked && "cursor-not-allowed opacity-50",
        )}
      >
        <p className="text-sm text-slate-300">
          {locked
            ? "Datasets are read-only once the Analyst has consumed them."
            : "Drop CSV, TSV, JSON, JSONL, or Parquet here"}
        </p>
        {!locked && (
          <div className="mt-3">
            <input
              ref={inputRef}
              type="file"
              accept={ALLOWED_EXT.join(",")}
              multiple
              disabled={busy}
              onChange={(e) => {
                if (e.target.files) void handleFiles(e.target.files);
              }}
              className="block w-full text-sm text-slate-400 file:mr-3 file:rounded-md file:border-0 file:bg-emerald-700 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-white hover:file:bg-emerald-600 disabled:opacity-50"
            />
          </div>
        )}
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-md border border-red-700 bg-red-950/40 px-3 py-2 text-sm text-red-300"
        >
          {error}
        </p>
      )}

      {datasets.length > 0 && (
        <ul className="divide-y divide-slate-800 rounded-md border border-slate-800">
          {datasets.map((d) => (
            <li
              key={d.id}
              className="flex items-center justify-between px-3 py-2 text-sm"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-slate-200">{d.filename}</p>
                <p className="mt-0.5 text-xs text-slate-500">
                  {d.rowcount.toLocaleString()} rows · {d.columns.length}{" "}
                  columns · {_formatBytes(d.bytes)}
                </p>
                {d.columns.length > 0 && (
                  <p
                    className="mt-1 truncate font-mono text-xs text-slate-400"
                    title={d.columns.join(", ")}
                  >
                    {d.columns.slice(0, 6).join(", ")}
                    {d.columns.length > 6 && ` … (+${d.columns.length - 6})`}
                  </p>
                )}
              </div>
              {!locked && (
                <button
                  type="button"
                  onClick={() => void handleDelete(d.id)}
                  disabled={busy}
                  className="ml-3 rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:border-red-700 hover:text-red-300 disabled:opacity-50"
                >
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
