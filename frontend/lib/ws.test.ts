// WebSocket reconnect-policy tests (Action Board P2 — "reconnect/backoff maps
// to backend close codes"). Locks the close-code → reconnect decision and the
// bounded-jitter backoff so a future edit can't silently make the client
// hammer the server (or give up when it should retry).

import { describe, expect, it } from "vitest";

import { _backoffDelay, shouldReconnect } from "./ws";

describe("shouldReconnect — close-code policy (mirrors backend SPEC §4)", () => {
  it("does NOT reconnect on auth/permission closes (4401, 4403)", () => {
    expect(shouldReconnect(4401)).toBe(false); // auth failed
    expect(shouldReconnect(4403)).toBe(false); // unauthorized project
  });

  it("does NOT reconnect on normal/policy closes (1000, 1008)", () => {
    expect(shouldReconnect(1000)).toBe(false); // normal closure
    expect(shouldReconnect(1008)).toBe(false); // policy violation
  });

  it("DOES reconnect on rate-limit (4429) — back off and retry, not give up", () => {
    expect(shouldReconnect(4429)).toBe(true);
  });

  it("DOES reconnect on transient transport closes (1006, 1011, 1001)", () => {
    expect(shouldReconnect(1006)).toBe(true); // abnormal closure
    expect(shouldReconnect(1011)).toBe(true); // server error
    expect(shouldReconnect(1001)).toBe(true); // going away
  });
});

describe("_backoffDelay — exponential with full jitter, capped", () => {
  it("stays within [0, min(cap, base*2^attempt)] at every attempt", () => {
    const BASE = 500;
    const CAP = 30_000;
    for (let attempt = 0; attempt < 12; attempt++) {
      const ceiling = Math.min(CAP, BASE * 2 ** attempt);
      for (let i = 0; i < 50; i++) {
        const d = _backoffDelay(attempt);
        expect(d).toBeGreaterThanOrEqual(0);
        expect(d).toBeLessThanOrEqual(ceiling);
      }
    }
  });

  it("is capped at 30s even for large attempt counts (no unbounded growth)", () => {
    for (let i = 0; i < 50; i++) {
      expect(_backoffDelay(20)).toBeLessThanOrEqual(30_000);
    }
  });
});
