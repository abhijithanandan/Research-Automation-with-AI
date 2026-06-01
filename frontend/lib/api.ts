// Typed REST client. Mirrors SPEC.md §3.
// Every API call must go through this file — do not call `fetch` from components.

import type {
  Artifact,
  CitationPanel,
  Dataset,
  ExportFormat,
  Paper,
  Project,
  UsageRollup,
  WorkflowRun,
} from "./types";

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type FetchOptions = RequestInit & { token?: string };

/** Bucketing the UI cares about. Used to choose a recovery action (sign in,
 * back off, validate input, retry) without parsing free-form error strings.
 * Lined up with FastAPI's status-code conventions and the SPEC §3.7 codes. */
export type ApiErrorKind =
  | "auth"        // 401/403 — sign-in or permission problem
  | "not_found"   // 404
  | "conflict"    // 409 — workflow phase locked
  | "validation"  // 422 — Pydantic validation failure
  | "rate_limit"  // 429
  | "server"      // 5xx
  | "network"     // fetch threw before getting a response
  | "unknown";

/** Typed API error — replaces the previous `throw new Error(JSON.stringify(...))`
 * pattern. UI code can `instanceof ApiError` and react to .kind / .status. */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;
  readonly code: string;
  readonly traceId?: string;
  readonly detail: unknown;

  constructor(
    kind: ApiErrorKind,
    status: number,
    code: string,
    message: string,
    detail: unknown,
    traceId?: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.traceId = traceId;
  }
}

// Exported for unit tests (lib/api.test.ts). Pure status→kind mapping.
export function classifyStatus(status: number): ApiErrorKind {
  if (status === 401 || status === 403) return "auth";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 422) return "validation";
  if (status === 429) return "rate_limit";
  if (status >= 500) return "server";
  return "unknown";
}

interface ErrorBody {
  error?: { code?: string; message?: string; trace_id?: string };
  detail?: unknown;
  message?: string;
}

// Exported for unit tests (lib/api.test.ts). Parses both error-envelope shapes.
export function extractError(status: number, body: unknown): ApiError {
  const e = (body ?? {}) as ErrorBody;
  // FastAPI/HTTPException uses `detail`; our app uses SPEC §3.7 `error: {code,
  // message, trace_id}`. Handle both shapes.
  const detailObj =
    typeof e.detail === "object" && e.detail !== null
      ? (e.detail as { code?: string; message?: string })
      : undefined;
  const code = e.error?.code ?? detailObj?.code ?? "unknown";
  const message =
    e.error?.message ?? detailObj?.message ?? e.message ?? `HTTP ${status}`;
  return new ApiError(
    classifyStatus(status),
    status,
    code,
    message,
    body,
    e.error?.trace_id,
  );
}

/** Download a binary attachment (Export Pack, FR-3.5). Returns the file's
 *  blob + the server-suggested filename (parsed from Content-Disposition).
 *  Errors flow through the same ApiError pipeline as request<T>(). */
export async function requestBlob(
  path: string,
  opts: FetchOptions = {},
): Promise<{ blob: Blob; filename: string }> {
  const { token, headers, ...rest } = opts;
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/api/v1${path}`, {
      ...rest,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
    });
  } catch (exc) {
    throw new ApiError(
      "network",
      0,
      "network_error",
      exc instanceof Error ? exc.message : "Network request failed",
      null,
    );
  }
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = { error: { code: "unknown", message: res.statusText } };
    }
    throw extractError(res.status, body);
  }
  // Content-Disposition: attachment; filename="manuscript-package.zip"
  const cd = res.headers.get("Content-Disposition") ?? "";
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const raw = m?.[1];
  const filename = raw ? decodeURIComponent(raw) : "download";
  return { blob: await res.blob(), filename };
}

async function request<T>(path: string, opts: FetchOptions = {}): Promise<T> {
  const { token, headers, ...rest } = opts;
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/api/v1${path}`, {
      ...rest,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
    });
  } catch (exc) {
    // fetch() throws on network failure (offline, DNS error, CORS preflight
    // rejection). Surface that as a typed network error so the UI can
    // distinguish it from server errors.
    throw new ApiError(
      "network",
      0,
      "network_error",
      exc instanceof Error ? exc.message : "Network request failed",
      null,
    );
  }
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = { error: { code: "unknown", message: res.statusText } };
    }
    throw extractError(res.status, body);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => request<{ status: string; version: string }>("/health"),

  projects: {
    create: (
      body: Pick<Project, "title" | "seed_query"> & {
        output_format?: "markdown" | "latex";
        token_cap_usd?: number;
      },
      token: string,
    ) =>
      request<Project>("/projects", {
        method: "POST",
        body: JSON.stringify(body),
        token,
      }),

    list: (token: string) =>
      request<Project[]>("/projects", { token }),

    get: (id: string, token: string) =>
      request<Project>(`/projects/${id}`, { token }),
  },

  workflow: {
    start: (projectId: string, token: string) =>
      request<WorkflowRun>(`/projects/${projectId}/workflow/start`, {
        method: "POST",
        token,
      }),

    approve: (
      projectId: string,
      body: {
        feedback?: string | null;
        force_unresolved?: boolean;
        override_reason?: string | null;
      },
      token: string,
    ) =>
      request<WorkflowRun>(`/projects/${projectId}/workflow/approve`, {
        method: "POST",
        body: JSON.stringify(body),
        token,
      }),

    reject: (projectId: string, feedback: string, token: string) =>
      request<WorkflowRun>(`/projects/${projectId}/workflow/reject`, {
        method: "POST",
        body: JSON.stringify({ feedback }),
        token,
      }),

    override: (
      projectId: string,
      body: {
        artifact_kind: string;
        label: string;
        content: string;
        mime_type?: string;
        citation_corrections?: Record<string, string>;
        override_reason?: string | null;
      },
      token: string,
    ) =>
      request<WorkflowRun>(`/projects/${projectId}/workflow/override`, {
        method: "POST",
        body: JSON.stringify(body),
        token,
      }),
  },

  drafting: {
    citations: (projectId: string, section: string, token: string) =>
      request<CitationPanel>(
        `/projects/${projectId}/drafting/citations?section=${encodeURIComponent(section)}`,
        { token },
      ),
  },

  papers: {
    list: (projectId: string, token: string) =>
      request<Paper[]>(`/projects/${projectId}/papers`, { token }),

    setApproved: (
      projectId: string,
      paperId: string,
      approved: boolean,
      token: string,
    ) =>
      request<Paper>(`/projects/${projectId}/papers/${paperId}`, {
        method: "PATCH",
        body: JSON.stringify({ approved }),
        token,
      }),
  },

  artifacts: {
    list: (projectId: string, kind: string | undefined, token: string) =>
      request<Artifact[]>(
        `/projects/${projectId}/artifacts${kind ? `?kind=${kind}` : ""}`,
        { token },
      ),
  },

  exports: {
    /** Download the manuscript in one of four formats (FR-3.5). Returns
     *  { blob, filename } — the caller saves it via an <a download> dance. */
    download: (projectId: string, format: ExportFormat, token: string) =>
      requestBlob(
        `/projects/${projectId}/export?format=${format}`,
        { token },
      ),
  },

  usage: {
    /** Token + cost rollup + Phase-4 drafting{} telemetry block (NFR-6 / §9). */
    get: (projectId: string, token: string) =>
      request<UsageRollup>(`/projects/${projectId}/usage`, { token }),
  },

  analysis: {
    /** Approve the Analyst's proposed code (optionally substituting an
     *  edited version). Server scans override_code against the AST denylist
     *  before resuming the graph; on a deny we get 422 with `code` =
     *  `code_static_scan_failed`. (SPEC v0.3 §3.3) */
    approveCode: (
      projectId: string,
      body: { feedback?: string | null; override_code?: string | null },
      token: string,
    ) =>
      request<WorkflowRun>(
        `/projects/${projectId}/workflow/analysis/approve-code`,
        { method: "POST", body: JSON.stringify(body), token },
      ),
    /** Reject the proposed code; feedback is required (the LLM uses it
     *  as the revision instruction when regenerating). */
    rejectCode: (projectId: string, feedback: string, token: string) =>
      request<WorkflowRun>(
        `/projects/${projectId}/workflow/analysis/reject-code`,
        { method: "POST", body: JSON.stringify({ feedback }), token },
      ),
    approveResults: (
      projectId: string,
      feedback: string | null,
      token: string,
    ) =>
      request<WorkflowRun>(
        `/projects/${projectId}/workflow/analysis/approve-results`,
        { method: "POST", body: JSON.stringify({ feedback }), token },
      ),
    rejectResults: (projectId: string, feedback: string, token: string) =>
      request<WorkflowRun>(
        `/projects/${projectId}/workflow/analysis/reject-results`,
        { method: "POST", body: JSON.stringify({ feedback }), token },
      ),
  },

  datasets: {
    /** List the project's uploaded datasets (newest first). Phase 3 / FR-2.3. */
    list: (projectId: string, token: string) =>
      request<Dataset[]>(`/projects/${projectId}/datasets`, { token }),

    /** Multipart upload. Returns the populated Dataset (with sha256, columns, rowcount). */
    upload: async (
      projectId: string,
      file: File,
      token: string,
    ): Promise<Dataset> => {
      const form = new FormData();
      form.append("file", file);
      // FormData uploads need to skip the JSON Content-Type the request()
      // helper sets, so go direct here.
      const res = await fetch(
        `${BASE_URL}/api/v1/projects/${projectId}/datasets/upload`,
        {
          method: "POST",
          body: form,
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!res.ok) {
        let detail: unknown;
        try {
          detail = await res.json();
        } catch {
          detail = { error: { code: "unknown", message: res.statusText } };
        }
        const errCode =
          (detail as { detail?: { code?: string } })?.detail?.code ??
          "upload_failed";
        const errMessage =
          (detail as { detail?: { message?: string } })?.detail?.message ??
          `Upload failed (${res.status})`;
        throw new ApiError(
          classifyStatus(res.status),
          res.status,
          errCode,
          errMessage,
          detail,
        );
      }
      return (await res.json()) as Dataset;
    },

    /** Remove a dataset. 409 once Phase 3 starts. */
    delete: (projectId: string, datasetId: string, token: string) =>
      request<void>(`/projects/${projectId}/datasets/${datasetId}`, {
        method: "DELETE",
        token,
      }),
  },
};
