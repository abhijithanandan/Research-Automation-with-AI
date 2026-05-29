// API error-typing tests (Action Board P2 — "normalize API error typing").
// Locks the status→kind mapping and the dual error-envelope parsing so the UI
// can always pick the right recovery action (sign in / back off / validate).

import { describe, expect, it } from "vitest";

import { ApiError, classifyStatus, extractError } from "./api";

describe("classifyStatus", () => {
  it("maps auth codes (401/403) to 'auth'", () => {
    expect(classifyStatus(401)).toBe("auth");
    expect(classifyStatus(403)).toBe("auth");
  });

  it("maps 404 → not_found, 409 → conflict, 422 → validation, 429 → rate_limit", () => {
    expect(classifyStatus(404)).toBe("not_found");
    expect(classifyStatus(409)).toBe("conflict");
    expect(classifyStatus(422)).toBe("validation");
    expect(classifyStatus(429)).toBe("rate_limit");
  });

  it("maps any 5xx to 'server'", () => {
    expect(classifyStatus(500)).toBe("server");
    expect(classifyStatus(502)).toBe("server");
    expect(classifyStatus(503)).toBe("server");
  });

  it("falls back to 'unknown' for unmapped codes", () => {
    expect(classifyStatus(418)).toBe("unknown");
  });
});

describe("extractError", () => {
  it("parses the SPEC §3.7 envelope { error: { code, message, trace_id } }", () => {
    const err = extractError(409, {
      error: { code: "phase_locked", message: "Phase is locked", trace_id: "01HX" },
    });
    expect(err).toBeInstanceOf(ApiError);
    expect(err.kind).toBe("conflict");
    expect(err.code).toBe("phase_locked");
    expect(err.message).toBe("Phase is locked");
    expect(err.traceId).toBe("01HX");
  });

  it("parses the FastAPI envelope { detail: { code, message } }", () => {
    const err = extractError(409, {
      detail: { code: "already_approved", message: "Already approved" },
    });
    expect(err.kind).toBe("conflict");
    expect(err.code).toBe("already_approved");
    expect(err.message).toBe("Already approved");
  });

  it("degrades gracefully when the body has no recognizable shape", () => {
    const err = extractError(500, { weird: true });
    expect(err.kind).toBe("server");
    expect(err.code).toBe("unknown");
    expect(err.message).toContain("500");
  });

  it("preserves the raw body and status on the error", () => {
    const body = { error: { code: "x" } };
    const err = extractError(403, body);
    expect(err.status).toBe(403);
    expect(err.detail).toBe(body);
  });
});
