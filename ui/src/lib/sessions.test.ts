// Tests for the session rail store (TD-1701).
//
// The store rides the connection fan-out for session_list/session_state and
// re-targets the single-session stores (chat pane, session status) when the
// user picks a row. These tests mock those seams the way workspaces.test.ts
// does and drive the reducer with protocol-shaped events.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, SessionSummary } from "./protocol";

// Captures the handlers the store registers, the messages it sends, and the
// re-targeting calls it makes into the single-session stores.
const mocks = vi.hoisted(() => {
  const eventHandlers = new Set<(e: DaemonEventUnion) => void>();
  const stateHandlers = new Set<(s: string) => void>();
  return {
    eventHandlers,
    stateHandlers,
    sent: [] as ClientMessageUnion[],
    sendResult: true,
    // Mocked connection + store state the rail reads.
    wsState: { state: "connected" },
    chatState: { sessionId: null as string | null },
    statusState: { workspacePath: null as string | null },
    // Recorded calls into the single-session stores.
    chatSelects: [] as Array<[id: string, turnState: string | null]>,
    focuses: [] as Array<{ id: string; state: string; workspacePath: string | undefined }>,
  };
});

vi.mock("./connection-status.svelte.js", () => ({
  onEvent: (handler: (e: DaemonEventUnion) => void) => {
    mocks.eventHandlers.add(handler);
    return () => {
      mocks.eventHandlers.delete(handler);
    };
  },
  onConnectionState: (handler: (s: string) => void) => {
    mocks.stateHandlers.add(handler);
    return () => {
      mocks.stateHandlers.delete(handler);
    };
  },
  sendToDaemon: (msg: ClientMessageUnion) => {
    mocks.sent.push(msg);
    return mocks.sendResult;
  },
  ws: mocks.wsState,
}));

vi.mock("./chat-store.svelte.js", () => ({
  chat: mocks.chatState,
  // Mirror the real selectSession: the pane's attached id moves synchronously.
  selectSession: (id: string, turnState: string | null) => {
    mocks.chatSelects.push([id, turnState]);
    mocks.chatState.sessionId = id;
  },
}));

vi.mock("./session-status.svelte.js", () => ({
  focusSession: (id: string, state: string, workspacePath?: string) => {
    mocks.focuses.push({ id, state, workspacePath });
  },
  session: mocks.statusState,
  workspaceName: (p: string) => p.split(/[\\/]/).filter((s) => s.length > 0).pop() ?? p,
}));

import {
  sessions,
  startSessions,
  resetSessions,
  visibleRows,
  setFilter,
  toggleCollapsed,
  selectRow,
  newSession,
  stateTone,
  recencyLabel,
  rowTitle,
  rowSubtitle,
  COLLAPSED_STORAGE_KEY,
} from "./sessions.svelte.js";

// ── Fixtures ───────────────────────────────────────────────────────────────

type SummaryState = SessionSummary["state"];

function sessionList(entries: Array<[id: string, updatedAt: string, state?: SummaryState, path?: string]>): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 1,
    sessions: entries.map(([id, updatedAt, state, path]) => ({
      session_id: id,
      workspace_path: path ?? "/ws/proj",
      state: state ?? "idle",
      created_at: updatedAt,
      updated_at: updatedAt,
      event_count: 3,
    })),
  };
}

function sessionState(sessionId: string, state: SummaryState): DaemonEventUnion {
  return { type: "session_state", seq: 5, session_id: sessionId, state };
}

function emit(event: DaemonEventUnion): void {
  for (const h of mocks.eventHandlers) h(event);
}

function transitions(state: string): void {
  for (const h of mocks.stateHandlers) h(state);
}

function sentTypes(): string[] {
  return mocks.sent.map((m) => m.type);
}

// Minimal localStorage stub (node test env has none).
const memory = new Map<string, string>();
const storageStub: Storage = {
  get length() {
    return memory.size;
  },
  clear: () => memory.clear(),
  getItem: (k: string) => memory.get(k) ?? null,
  key: (i: number) => [...memory.keys()][i] ?? null,
  removeItem: (k: string) => void memory.delete(k),
  setItem: (k: string, v: string) => void memory.set(k, v),
};

beforeEach(() => {
  vi.stubGlobal("localStorage", storageStub);
  memory.clear();
  mocks.eventHandlers.clear();
  mocks.stateHandlers.clear();
  mocks.sent.length = 0;
  mocks.chatSelects.length = 0;
  mocks.focuses.length = 0;
  mocks.chatState.sessionId = null;
  mocks.statusState.workspacePath = null;
  mocks.sendResult = true;
  mocks.wsState.state = "connected";
  resetSessions();
  startSessions();
  mocks.sent.length = 0; // drop the start-time refresh from assertions
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ── Listing and ordering (AC: live + interrupted, newest first) ────────────

describe("list reduction", () => {
  it("orders rows newest first by updated_at and keeps every state", () => {
    emit(
      sessionList([
        ["old-idle", "2026-08-10T00:00:00Z", "idle"],
        ["new-run", "2026-08-14T09:00:00Z", "running"],
        ["mid-int", "2026-08-12T00:00:00Z", "interrupted"],
      ]),
    );
    expect(sessions.rows.map((r) => r.sessionId)).toEqual(["new-run", "mid-int", "old-idle"]);
    expect(sessions.rows.map((r) => r.state)).toEqual(["running", "interrupted", "idle"]);
  });

  it("requests the list when it mounts on a live connection", () => {
    resetSessions();
    startSessions();
    expect(sentTypes()).toEqual(["list_sessions"]);
  });

  it("mounts silent while disconnected and fetches when the connection comes up", () => {
    mocks.wsState.state = "disconnected";
    // Re-register cleanly: drop the beforeEach registrations first.
    mocks.eventHandlers.clear();
    mocks.stateHandlers.clear();
    resetSessions();
    startSessions();
    expect(sentTypes()).toEqual([]);
    transitions("connected");
    expect(sentTypes()).toEqual(["list_sessions"]);
  });
});

describe("live state touches", () => {
  it("updates a row's state from session_state and refreshes the list", () => {
    emit(sessionList([["s1", "2026-08-14T09:00:00Z", "running"]]));
    emit(sessionState("s1", "complete"));
    expect(sessions.rows.find((r) => r.sessionId === "s1")?.state).toBe("complete");
    expect(sentTypes()).toEqual(["list_sessions"]);
  });

  it("coalesces repeated transitions into one in-flight refresh", () => {
    emit(sessionList([["s1", "2026-08-14T09:00:00Z", "running"]]));
    emit(sessionState("s1", "awaiting_approval"));
    emit(sessionState("s1", "running"));
    expect(sentTypes()).toEqual(["list_sessions"]);
  });

  it("refreshes when a session_state names a session the list doesn't know", () => {
    emit(sessionList([["s1", "2026-08-14T09:00:00Z", "idle"]]));
    emit(sessionState("somewhere-else", "running"));
    expect(sentTypes()).toEqual(["list_sessions"]);
  });
});

// ── Filter (AC: narrows the list client-side) ──────────────────────────────

describe("filter", () => {
  beforeEach(() => {
    emit(
      sessionList([
        ["abc12345-rest", "2026-08-14T09:00:00Z", "idle", "/ws/api-server"],
        ["def67890-rest", "2026-08-13T09:00:00Z", "interrupted", "/ws/web-app"],
      ]),
    );
  });

  it("empty filter shows everything", () => {
    expect(visibleRows()).toHaveLength(2);
  });

  it("matches the session id, case-insensitively", () => {
    setFilter("ABC123");
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["abc12345-rest"]);
  });

  it("matches the workspace path", () => {
    setFilter("web");
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["def67890-rest"]);
  });

  it("no match yields an empty list without touching the rows", () => {
    setFilter("zzz");
    expect(visibleRows()).toEqual([]);
    expect(sessions.rows).toHaveLength(2);
  });
});

// ── Attach selection (AC: click attaches; attached is marked by the pane) ──

describe("row selection", () => {
  beforeEach(() => {
    emit(
      sessionList([
        ["s1", "2026-08-14T09:00:00Z", "running", "/ws/proj"],
        ["s2", "2026-08-13T09:00:00Z", "interrupted", "/ws/proj"],
      ]),
    );
    mocks.chatState.sessionId = "s1";
  });

  it("attaches the chat pane and focuses the status store with row data", () => {
    selectRow("s2");
    expect(mocks.chatSelects).toEqual([["s2", "interrupted"]]);
    expect(mocks.focuses).toEqual([{ id: "s2", state: "interrupted", workspacePath: "/ws/proj" }]);
  });

  it("ignores the already-attached session and unknown ids", () => {
    selectRow("s1");
    selectRow("nope");
    expect(mocks.chatSelects).toEqual([]);
    expect(mocks.focuses).toEqual([]);
  });
});

// ── New session (AC: creates and attaches in the current workspace) ────────

describe("new session", () => {
  beforeEach(() => {
    emit(sessionList([["s1", "2026-08-14T09:00:00Z", "complete", "/ws/proj"]]));
    mocks.chatState.sessionId = "s1";
    mocks.statusState.workspacePath = "/ws/proj";
  });

  it("sends new_session anchored on the attached session", () => {
    expect(newSession()).toBe(true);
    expect(mocks.sent).toEqual([{ type: "new_session", session_id: "s1" }]);
  });

  it("focuses the fresh session when its first session_state lands", () => {
    newSession();
    emit(sessionState("fresh-id", "running"));
    expect(mocks.chatSelects).toEqual([["fresh-id", "running"]]);
    // Workspace comes from the anchor's row — the new session shares it.
    expect(mocks.focuses).toEqual([{ id: "fresh-id", state: "running", workspacePath: "/ws/proj" }]);
    // …and the list refreshes so the new row appears promptly.
    expect(sentTypes()).toEqual(["new_session", "list_sessions"]);
  });

  it("does not treat the anchor's own transitions as the new session", () => {
    newSession();
    emit(sessionState("s1", "running"));
    expect(mocks.chatSelects).toEqual([]);
  });

  it("refuses without an attached session or while one is in flight", () => {
    mocks.chatState.sessionId = null;
    expect(newSession()).toBe(false);
    expect(sentTypes()).toEqual([]);
    mocks.chatState.sessionId = "s1";
    expect(newSession()).toBe(true);
    expect(newSession()).toBe(false); // still awaiting the reply
    expect(sentTypes()).toEqual(["new_session"]);
  });

  it("stays unfocused when the send fails", () => {
    mocks.sendResult = false;
    expect(newSession()).toBe(false);
    emit(sessionState("fresh-id", "running"));
    expect(mocks.chatSelects).toEqual([]);
  });
});

// ── Collapse persistence (AC: collapsed state persists) ───────────────────

describe("collapse", () => {
  it("toggles and persists", () => {
    expect(sessions.collapsed).toBe(false);
    toggleCollapsed();
    expect(sessions.collapsed).toBe(true);
    expect(memory.get(COLLAPSED_STORAGE_KEY)).toBe("1");
  });

  it("restores the collapsed flag on start", () => {
    memory.set(COLLAPSED_STORAGE_KEY, "1");
    resetSessions();
    startSessions();
    expect(sessions.collapsed).toBe(true);
  });

  it("survives unavailable storage", () => {
    vi.unstubAllGlobals(); // no localStorage at all
    resetSessions();
    startSessions();
    expect(sessions.collapsed).toBe(false);
    toggleCollapsed();
    expect(sessions.collapsed).toBe(true);
  });
});

// ── Row presentation ───────────────────────────────────────────────────────

describe("presentation helpers", () => {
  it("stateTone matches the title bar's indicator mapping", () => {
    expect(stateTone("running")).toBe("info");
    expect(stateTone("awaiting_approval")).toBe("warning");
    expect(stateTone("paused")).toBe("warning");
    expect(stateTone("failed")).toBe("danger");
    expect(stateTone("complete")).toBe("success");
    expect(stateTone("interrupted")).toBe("muted");
  });

  it("recency labels compress to now/m/h/d then a short date", () => {
    const now = Date.parse("2026-08-14T12:00:00Z");
    expect(recencyLabel("2026-08-14T11:59:40Z", now)).toBe("now");
    expect(recencyLabel("2026-08-14T11:41:00Z", now)).toBe("19m");
    expect(recencyLabel("2026-08-14T07:00:00Z", now)).toBe("5h");
    expect(recencyLabel("2026-08-12T12:00:00Z", now)).toBe("2d");
    expect(recencyLabel("2026-07-01T12:00:00Z", now)).toBe("Jul 1");
    expect(recencyLabel("not-a-date", now)).toBe("");
  });

  it("row title is the short id; subtitle names the workspace and the recency", () => {
    const now = Date.parse("2026-08-14T12:00:00Z");
    const row = {
      sessionId: "abc12345-0000-0000-0000-000000000000",
      workspacePath: "/ws/api-server",
      state: "idle" as SummaryState,
      updatedAt: "2026-08-14T11:00:00Z",
    };
    expect(rowTitle(row)).toBe("abc12345");
    expect(rowSubtitle(row, now)).toBe("api-server · 1h");
  });
});
