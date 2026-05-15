// WebSocket client. Mirrors SPEC.md §4.
// Discriminated union of server→client events. Add new event types here AND in SPEC.md.

import type { Phase, WorkflowState } from "./types";

export type ServerEvent =
  | { type: "auth.ok"; ts: string }
  | { type: "state.changed"; ts: string; phase: Phase; state: WorkflowState; run_id: string }
  | { type: "agent.started"; ts: string; agent: string; run_id: string }
  | { type: "agent.token"; ts: string; agent: string; run_id: string; delta: string }
  | { type: "agent.completed"; ts: string; agent: string; run_id: string; artifact_ids: string[] }
  | { type: "agent.error"; ts: string; agent: string; run_id: string; error: string }
  | { type: "approval.required"; ts: string; phase: Phase; run_id: string; summary: string }
  | { type: "usage.tick"; ts: string; tokens_in: number; tokens_out: number; cost_usd: number }
  | { type: "pong"; ts: string };

export interface WSOptions {
  projectId: string;
  token: string;
  onEvent: (e: ServerEvent) => void;
  onError?: (err: Event) => void;
  onClose?: (e: CloseEvent) => void;
}

export function connectProjectEvents(opts: WSOptions): WebSocket {
  const base =
    process.env.NEXT_PUBLIC_WS_BASE_URL ?? "ws://localhost:8000";
  const ws = new WebSocket(`${base}/api/v1/projects/${opts.projectId}/events`);

  let heartbeat: ReturnType<typeof setInterval> | null = null;

  ws.addEventListener("open", () => {
    ws.send(JSON.stringify({ type: "auth", token: opts.token }));
    heartbeat = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
      }
    }, 30_000);
  });

  ws.addEventListener("message", (msg) => {
    try {
      const evt = JSON.parse(msg.data) as ServerEvent;
      opts.onEvent(evt);
    } catch {
      // Malformed event — log and drop. Server is the source of truth.
    }
  });

  if (opts.onError) ws.addEventListener("error", opts.onError);
  ws.addEventListener("close", (e) => {
    if (heartbeat) clearInterval(heartbeat);
    opts.onClose?.(e);
  });

  return ws;
}
