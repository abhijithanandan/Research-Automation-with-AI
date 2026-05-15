// Typed REST client. Mirrors SPEC.md §3.
// Every API call must go through this file — do not call `fetch` from components.

import type { Artifact, Paper, Project } from "./types";

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type FetchOptions = RequestInit & { token?: string };

async function request<T>(path: string, opts: FetchOptions = {}): Promise<T> {
  const { token, headers, ...rest } = opts;
  const res = await fetch(`${BASE_URL}/api/v1${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = { error: { code: "unknown", message: res.statusText } };
    }
    throw new Error(
      `API ${res.status}: ${JSON.stringify(detail)}`,
    );
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

    approve: (projectId: string, feedback: string | null, token: string) =>
      request<unknown>(`/projects/${projectId}/workflow/approve`, {
        method: "POST",
        body: JSON.stringify({ feedback }),
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
      },
      token: string,
    ) =>
      request<unknown>(`/projects/${projectId}/workflow/override`, {
        method: "POST",
        body: JSON.stringify(body),
        token,
      }),
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
