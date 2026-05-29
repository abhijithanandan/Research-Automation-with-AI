// WebSocket client. Mirrors SPEC.md §4.
// Discriminated union of server→client events. Add new event types here AND in SPEC.md.

import type { Phase, SectionName, WorkflowState } from "./types";

export type ServerEvent =
  | { type: "auth.ok"; ts: string }
  | { type: "state.changed"; ts: string; phase: Phase; state: WorkflowState; run_id: string }
  | { type: "agent.started"; ts: string; agent: string; run_id: string }
  | { type: "agent.token"; ts: string; agent: string; run_id: string; delta: string }
  | { type: "agent.completed"; ts: string; agent: string; run_id: string; artifact_ids: string[] }
  | { type: "agent.error"; ts: string; agent: string; run_id: string; error: string }
  | {
      type: "approval.required";
      ts: string;
      phase: Phase;
      run_id: string;
      summary: string;
      // `section` is present only when phase === "drafting" — identifies
      // which of the seven canonical sections is up for review (SPEC §4.1).
      section?: SectionName;
    }
  | { type: "usage.tick"; ts: string; tokens_in: number; tokens_out: number; cost_usd: number }
  | {
      // Emitted when project spend crosses token_cap_warn_pct of the cap
      // (NFR-5). Advisory — the workflow keeps running.
      type: "cost.cap_warn";
      ts: string;
      run_id: string;
      spend_usd: number;
      cap_usd: number;
      warn_pct: number;
    }
  | {
      // Emitted when project spend reaches the cap (NFR-5). The run is moved
      // to "error"; the user must raise the cap to continue.
      type: "cost.cap_exceeded";
      ts: string;
      run_id: string;
      spend_usd: number;
      cap_usd: number;
    }
  | { type: "pong"; ts: string };

export interface WSOptions {
  projectId: string;
  token: string;
  onEvent: (e: ServerEvent) => void;
  onError?: (err: Event) => void;
  onClose?: (e: CloseEvent) => void;
  /** Called when reconnect attempts begin / a reconnect succeeds. */
  onReconnect?: (info: { attempt: number; nextDelayMs: number | null }) => void;
}

/** Reconnect-aware WebSocket handle returned by connectProjectEvents.
 *
 * Wraps a single logical connection that may transparently re-establish
 * the underlying socket after an unclean close. Callers see one stable
 * `close()` handle for the lifetime of their subscription. */
export interface ManagedSocket {
  /** Close permanently — disables reconnect loop. Matches WebSocket.close(). */
  close(code?: number, reason?: string): void;
  /** Current underlying socket (may be null between reconnect attempts). */
  readonly socket: WebSocket | null;
}

// Backoff parameters tuned per WebSocket reconnection best practice:
// start fast (instant retry feels responsive), cap at 30s (avoids hammering
// the server), randomise to prevent thundering-herd reconnects across tabs.
const _RECONNECT_INITIAL_MS = 500;
const _RECONNECT_MAX_MS = 30_000;
const _RECONNECT_MAX_ATTEMPTS = 10;
// Close codes that mean "do not reconnect" — auth failures, normal closures.
// NOTE: 4429 (server-side handshake rate limit, audit round-4 LOW-1) is
// intentionally NOT in this set — it means "back off and try again," which
// is exactly what the exponential-backoff path does.
const _NO_RECONNECT_CODES = new Set([
  1000, // normal closure
  1008, // policy violation
  4401, // our app-defined auth-failed code
  4403, // our app-defined unauthorized code
]);

/** Reconnect decision for a given WS close code. Exported (test-only) so the
 * code↔policy mapping can be asserted in isolation: auth/normal closures give
 * up; 4429 (rate-limit) and transient transport closes retry with backoff.
 * Mirrors the backend close-code contract (SPEC §4): 4401 auth, 4429 limit. */
export function shouldReconnect(code: number): boolean {
  return !_NO_RECONNECT_CODES.has(code);
}

export function _backoffDelay(attempt: number): number {
  // Exponential backoff with full jitter: delay = random(0, min(cap, base * 2^attempt))
  const expo = Math.min(_RECONNECT_MAX_MS, _RECONNECT_INITIAL_MS * 2 ** attempt);
  return Math.floor(Math.random() * expo);
}

export function connectProjectEvents(opts: WSOptions): ManagedSocket {
  const base = process.env.NEXT_PUBLIC_WS_BASE_URL ?? "ws://localhost:8000";
  const url = `${base}/api/v1/projects/${opts.projectId}/events`;

  let stopped = false;
  let currentSocket: WebSocket | null = null;
  let heartbeat: ReturnType<typeof setInterval> | null = null;
  let attempt = 0;

  function clearHeartbeat() {
    if (heartbeat !== null) {
      clearInterval(heartbeat);
      heartbeat = null;
    }
  }

  function open(): void {
    if (stopped) return;
    const ws = new WebSocket(url);
    currentSocket = ws;

    ws.addEventListener("open", () => {
      // Reset attempts on a successful connection — the next disconnect
      // starts the backoff cycle over from 500ms again.
      if (attempt > 0) opts.onReconnect?.({ attempt, nextDelayMs: null });
      attempt = 0;
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
      clearHeartbeat();
      opts.onClose?.(e);

      // Reconnect decision matrix:
      //   - User asked to stop → never reconnect.
      //   - Server signalled "do not reconnect" close code → stop.
      //   - Out of retry budget → stop.
      //   - Otherwise, exponential backoff + jitter → retry.
      if (stopped || !shouldReconnect(e.code)) return;
      if (attempt >= _RECONNECT_MAX_ATTEMPTS) {
        opts.onReconnect?.({ attempt, nextDelayMs: null });
        return;
      }
      const delay = _backoffDelay(attempt);
      attempt += 1;
      opts.onReconnect?.({ attempt, nextDelayMs: delay });
      setTimeout(open, delay);
    });
  }

  open();

  return {
    close(code = 1000, reason = "client close") {
      stopped = true;
      clearHeartbeat();
      currentSocket?.close(code, reason);
    },
    get socket() {
      return currentSocket;
    },
  };
}
