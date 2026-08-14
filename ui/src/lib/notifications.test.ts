// Tests for the notification store and error copy (TD-1008).
//
// Toasts vs banners come straight from the copy table, so the suite drives
// the store with protocol-shaped events: failed turns, cap pauses, daemon
// errors — then checks severity routing, dedupe by cause, auto-expiry, and
// the redaction rules on the diagnostics report.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { DaemonEventUnion } from "./protocol";
import { sessionStateCopy, turnFailureCopy } from "./error-copy";
import {
  TOAST_TIMEOUT_MS,
  banners,
  buildDiagnostics,
  clearNotifications,
  copyDiagnostics,
  dismiss,
  notify,
  notifyEvent,
  toasts,
} from "./notifications.svelte.js";
import { ingestEvent, resetSession } from "./session-status.svelte.js";

function turnComplete(errorCode: string | null): DaemonEventUnion {
  return {
    type: "turn_complete",
    session_id: "s1",
    tokens: 0,
    cost: 0,
    tier: "brain",
    duration: 0.1,
    failed: errorCode !== null,
    error_code: errorCode,
    seq: 9,
  } as DaemonEventUnion;
}

function sessionState(state: string, reason?: string): DaemonEventUnion {
  return {
    type: "session_state",
    session_id: "s1",
    state,
    reason,
    seq: 10,
  } as DaemonEventUnion;
}

beforeEach(() => {
  clearNotifications();
  resetSession();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("error copy", () => {
  it("missing key and auth failure are banners naming the fix", () => {
    for (const code of ["missing_api_key", "auth_failed"] as const) {
      const spec = turnFailureCopy(code);
      expect(spec?.severity).toBe("banner");
      expect(spec?.body).toContain("tstd keychain set");
    }
  });

  it("transient provider failures are toasts", () => {
    for (const code of ["rate_limited", "server_error", "bad_gateway", "service_unavailable", "gateway_timeout"]) {
      expect(turnFailureCopy(code)?.severity).toBe("toast");
    }
  });

  it("a cancelled turn and a null code stay silent", () => {
    expect(turnFailureCopy("cancelled")).toBeNull();
    expect(turnFailureCopy(null)).toBeNull();
  });

  it("unknown codes still produce actionable copy, never a traceback", () => {
    const spec = turnFailureCopy("weird_new_code");
    expect(spec?.severity).toBe("toast");
    expect(spec?.body).toContain("weird_new_code");
    expect(spec?.body.toLowerCase()).not.toContain("traceback");
  });

  it("cap pauses carry the daemon's reason and point at the config", () => {
    for (const prefix of ["spend", "wall-clock", "iteration"]) {
      const spec = sessionStateCopy("paused", `${prefix} cap exceeded: 1 >= 0.5`);
      expect(spec?.severity).toBe("banner");
      expect(spec?.body).toContain(`${prefix} cap exceeded`);
      expect(spec?.body).toContain("config.yaml");
    }
  });

  it("a failed session is a banner; normal states stay quiet", () => {
    expect(sessionStateCopy("failed", "boom")?.severity).toBe("banner");
    expect(sessionStateCopy("running", null)).toBeNull();
    expect(sessionStateCopy("interrupted", null)).toBeNull();
  });
});

describe("notification routing", () => {
  it("a failed turn raises a banner for auth failures", () => {
    notifyEvent(turnComplete("auth_failed"));
    expect(banners).toHaveLength(1);
    expect(banners[0].title).toBe("API key rejected");
    expect(toasts).toHaveLength(0);
  });

  it("a transient turn failure raises a self-expiring toast", () => {
    notifyEvent(turnComplete("rate_limited"));
    expect(toasts).toHaveLength(1);
    expect(banners).toHaveLength(0);

    vi.advanceTimersByTime(TOAST_TIMEOUT_MS + 1);
    expect(toasts).toHaveLength(0);
  });

  it("repeats of the same cause update in place instead of stacking", () => {
    notifyEvent(turnComplete("rate_limited"));
    notifyEvent(turnComplete("rate_limited"));
    notifyEvent(turnComplete("rate_limited"));
    expect(toasts).toHaveLength(1);

    // A repeated toast restarts its timer rather than expiring early.
    vi.advanceTimersByTime(TOAST_TIMEOUT_MS - 100);
    expect(toasts).toHaveLength(1);
    vi.advanceTimersByTime(200);
    expect(toasts).toHaveLength(0);
  });

  it("a cap pause is a banner that resume clears", () => {
    notifyEvent(sessionState("paused", "spend cap exceeded: $1.02 >= $1.00"));
    expect(banners).toHaveLength(1);
    expect(banners[0].title).toBe("Spend cap reached");

    notifyEvent(sessionState("running"));
    expect(banners).toHaveLength(0);
  });

  it("resuming also clears turn-failure banners — work is flowing again", () => {
    notifyEvent(turnComplete("missing_api_key"));
    expect(banners).toHaveLength(1);
    notifyEvent(sessionState("running"));
    expect(banners).toHaveLength(0);
  });

  it("a user-initiated cancel raises nothing", () => {
    notifyEvent(turnComplete(null));
    expect(toasts).toHaveLength(0);
    expect(banners).toHaveLength(0);
  });

  it("daemon errors become toasts carrying the code", () => {
    notifyEvent({ type: "error", code: "bad_request", message: "unknown message type", seq: 3 } as DaemonEventUnion);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].title).toContain("bad_request");
    expect(toasts[0].body).toBe("unknown message type");
  });

  it("dismiss removes a banner immediately", () => {
    notifyEvent(turnComplete("auth_failed"));
    const id = banners[0].id;
    dismiss(id);
    expect(banners).toHaveLength(0);
  });
});

describe("diagnostics", () => {
  it("buildDiagnostics redacts paths and content but keeps state and events", () => {
    const running = sessionState("running");
    notifyEvent({ type: "ready", version: "0.3.1", protocol_version: 1, seq: 0 } as DaemonEventUnion);
    // The real sink feeds both reducers; drive the session store directly.
    ingestEvent(running);
    notifyEvent(running);
    notifyEvent(turnComplete("auth_failed"));
    const report = buildDiagnostics({ ws: "connected", daemonState: "connected", daemonRestart: 0 });

    expect(report).toContain("daemon 0.3.1");
    expect(report).toContain("protocol 1");
    expect(report).toContain("ws connected");
    expect(report).toContain("session: running");
    expect(report).toContain("turn_complete#9");
    // The redaction contract: no absolute paths, no message bodies.
    expect(report).not.toContain("/Users");
    expect(report).not.toContain("Authentication failed");
  });

  it("copyDiagnostics writes the report through the clipboard", async () => {
    notifyEvent({ type: "ready", version: "0.3.1", protocol_version: 1, seq: 0 } as DaemonEventUnion);
    const written: string[] = [];
    const ok = await copyDiagnostics(
      { ws: "connected", daemonState: "connected", daemonRestart: 0 },
      { writeText: (text: string) => Promise.resolve(void written.push(text)) },
    );
    expect(ok).toBe(true);
    expect(written).toHaveLength(1);
    expect(written[0]).toContain("tst-desk diagnostics");
  });

  it("copyDiagnostics returns false when no clipboard exists", async () => {
    const ok = await copyDiagnostics({ ws: "disconnected", daemonState: "stopped", daemonRestart: 0 }, undefined);
    expect(ok).toBe(false);
  });

  it("a clipboard rejection degrades to false, not a throw", async () => {
    const failing = { writeText: () => Promise.reject(new Error("denied")) };
    const ok = await copyDiagnostics({ ws: "connected", daemonState: "connected", daemonRestart: 0 }, failing);
    expect(ok).toBe(false);
  });
});
