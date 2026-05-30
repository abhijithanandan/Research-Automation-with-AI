// Typed REST client. Mirrors SPEC.md §3.
// Every API call must go through this file — do not call `fetch` from components.

import type { Artifact, CitationPanel, Paper, Project } from "./types";

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
      request<unknown>(`/projects/${projectId}/workflow/start`, {
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
      request<unknown>(`/projects/${projectId}/workflow/approve`, {
        method: "POST",
        body: JSON.stringify(body),
        token,
      }),

    reject: (projectId: string, feedback: string, token: string) =>
      request<unknown>(`/projects/${projectId}/workflow/reject`, {
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
      request<unknown>(`/projects/${projectId}/workflow/override`, {
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
};
