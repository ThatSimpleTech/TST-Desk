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
    // Recorded open_workspace calls from newSessionInWorkspace (TD-2801).
    opened: [] as string[],
    // Recorded calls the rail's function entries make (TD-1712).
    surfaceCalls: [] as string[],
    // Recorded re-points of the title bar after a move (TD-1715).
    retargets: [] as Array<{ id: string; workspacePath: string }>,
    // The recents store the move picker reads (TD-1715).
    workspacesState: { entries: [] as Array<{ path: string; lastSeen: string }> },
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
  // TD-1715: a moved session keeps its binding, so the title bar is
  // re-pointed rather than re-focused.
  retargetWorkspace: (id: string, workspacePath: string) => {
    mocks.retargets.push({ id, workspacePath });
  },
  openWorkspace: (path: string) => {
    mocks.opened.push(path);
  },
  session: mocks.statusState,
  workspaceName: (p: string) => p.split(/[\\/]/).filter((s) => s.length > 0).pop() ?? p,
}));

// The seam the rail's function entries drive (TD-1712). Recording it is how
// "never a dead click" is asserted in both directions: a ready entry moves
// something, every other entry moves nothing. `workspaces` is the recents
// store the move-to-project picker reads its targets from (TD-1715).
vi.mock("./workspaces.svelte.js", () => ({
  toggleWorkspaceMenu: () => void mocks.surfaceCalls.push("toggleWorkspaceMenu"),
  workspaces: mocks.workspacesState,
}));

import {
  sessions,
  startSessions,
  resetSessions,
  visibleRows,
  shelfRowCount,
  setFilter,
  toggleCollapsed,
  closeRowMenus,
  selectRow,
  newSession,
  newSessionInWorkspace,
  activateRailFunction,
  stateTone,
  recencyLabel,
  rowTitle,
  rowSubtitle,
  COLLAPSED_STORAGE_KEY,
} from "./sessions.svelte.js";
import {
  confirmDelete,
  moveRow,
  moveTargets,
  requestDelete,
  requestMove,
  setArchived,
  setStarred,
  toggleArchivedView,
  toggleStarredOnly,
  toggleRowMenu,
} from "./session-actions.svelte.js";
import { railFunctions } from "./rail";
import { projects, resetProjects, showProjects } from "./projects.svelte.js";

// ── Fixtures ───────────────────────────────────────────────────────────────

type SummaryState = SessionSummary["state"];

function sessionList(
  entries: Array<
    [id: string, updatedAt: string, state?: SummaryState, path?: string, archived?: boolean, starred?: boolean]
  >,
): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 1,
    sessions: entries.map(([id, updatedAt, state, path, archived, starred]) => ({
      session_id: id,
      workspace_path: path ?? "/ws/proj",
      state: state ?? "idle",
      created_at: updatedAt,
      updated_at: updatedAt,
      event_count: 3,
      archived: archived ?? false,
      starred: starred ?? false,
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
  mocks.surfaceCalls.length = 0;
  mocks.retargets.length = 0;
  mocks.opened.length = 0;
  mocks.workspacesState.entries = [];
  resetProjects();
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

  it("refuses while one is in flight", () => {
    expect(newSession()).toBe(true);
    expect(newSession()).toBe(false); // still awaiting the reply
    expect(sentTypes()).toEqual(["new_session"]);
  });

  it("anchors on the newest listed row when nothing is bound (TD-1711)", () => {
    // After a restart with only dead sessions, auto-bind stays unbound —
    // New Session is the escape, anchored on a listed (possibly terminal)
    // session purely for its workspace.
    mocks.chatState.sessionId = null;
    expect(newSession()).toBe(true);
    expect(mocks.sent).toEqual([{ type: "new_session", session_id: "s1" }]);
    // …and focus still moves to the fresh session's first session_state.
    emit(sessionState("fresh-id", "running"));
    expect(mocks.chatSelects).toEqual([["fresh-id", "running"]]);
  });

  it("refuses when nothing is bound and the list is empty", () => {
    mocks.chatState.sessionId = null;
    emit(sessionList([]));
    expect(newSession()).toBe(false);
    expect(sentTypes()).toEqual([]);
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

// ── Rail function entries (TD-1712 AC: never a dead click) ────────────────

describe("rail function entries", () => {
  it("refuses a surface whose epic hasn't landed, and touches nothing", () => {
    expect(activateRailFunction("scheduled")).toBe(false);
    expect(mocks.surfaceCalls).toEqual([]);
    expect(sentTypes()).toEqual([]);
  });

  it("refuses the surface the window is already on, and touches nothing", () => {
    expect(activateRailFunction("home")).toBe(false);
    expect(mocks.surfaceCalls).toEqual([]);
    expect(sentTypes()).toEqual([]);
  });

  it("refuses an id the registry doesn't list", () => {
    // @ts-expect-error — the guard has to hold for a caller that ignores types.
    expect(activateRailFunction("dispatch")).toBe(false);
    expect(mocks.surfaceCalls).toEqual([]);
  });

  it("activates every entry the registry calls ready", () => {
    // Marking an entry ready without wiring it fails here rather than
    // shipping a button that does nothing. Ready depends on the surface.
    for (const surface of ["home", "projects"] as const) {
      for (const entry of railFunctions(surface)) {
        if (entry.state !== "ready") continue;
        resetProjects();
        if (surface === "projects") showProjects();
        expect(activateRailFunction(entry.id)).toBe(true);
      }
    }
  });

  it("Projects opens the project list, not the title-bar recents menu", () => {
    expect(activateRailFunction("projects")).toBe(true);
    expect(projects.surface).toBe("projects");
    expect(projects.selectedPath).toBeNull();
    expect(mocks.surfaceCalls).toEqual([]);
  });

  it("Home is ready once Projects is showing, and takes you back", () => {
    showProjects();
    expect(activateRailFunction("home")).toBe(true);
    expect(projects.surface).toBe("home");
    expect(activateRailFunction("home")).toBe(false);
    expect(activateRailFunction("projects")).toBe(true);
  });
});

// ── New chat on a project home (TD-2801) ──────────────────────────────────

describe("newSessionInWorkspace", () => {
  beforeEach(() => {
    emit(
      sessionList([
        ["s1", "2026-08-14T09:00:00Z", "complete", "/ws/proj"],
        ["s2", "2026-08-14T10:00:00Z", "idle", "/ws/other"],
      ]),
    );
    mocks.sent.length = 0;
  });

  it("sends new_session anchored on a session in that workspace", () => {
    expect(newSessionInWorkspace("/ws/other")).toBe(true);
    expect(mocks.sent).toEqual([{ type: "new_session", session_id: "s2" }]);
    expect(projects.surface).toBe("home");
  });

  it("opens the workspace when it has no session to anchor", () => {
    expect(newSessionInWorkspace("/ws/fresh")).toBe(true);
    expect(mocks.opened).toEqual(["/ws/fresh"]);
    expect(sentTypes()).toEqual([]);
    expect(projects.surface).toBe("home");
  });

  it("refuses a second new_session while one is in flight", () => {
    expect(newSessionInWorkspace("/ws/proj")).toBe(true);
    expect(newSessionInWorkspace("/ws/proj")).toBe(false);
    expect(sentTypes()).toEqual(["new_session"]);
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
      archived: false,
      starred: false,
    };
    expect(rowTitle(row)).toBe("abc12345");
    expect(rowSubtitle(row, now)).toBe("api-server · 1h");
  });
});

// ── Archive, delete, move (TD-1715) ───────────────────────────────────────
//
// The rail's half of the story. The daemon owns durability and every refusal;
// what is asserted here is that the rail sends the right verb, shows the right
// shelf, and never invents an answer the daemon didn't give.

describe("archived shelf", () => {
  it("hides archived rows from the default list", () => {
    emit(
      sessionList([
        ["live", "2026-08-14T09:00:00Z", "idle"],
        ["filed", "2026-08-14T10:00:00Z", "idle", "/ws/proj", true],
      ]),
    );
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["live"]);
  });

  it("shows exactly the archived rows on the archived shelf", () => {
    emit(
      sessionList([
        ["live", "2026-08-14T09:00:00Z", "idle"],
        ["filed", "2026-08-14T10:00:00Z", "idle", "/ws/proj", true],
      ]),
    );
    toggleArchivedView();
    expect(sessions.showArchived).toBe(true);
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["filed"]);
  });

  it("keeps the archived flag as daemon truth, never inferred", () => {
    emit(sessionList([["filed", "2026-08-14T10:00:00Z", "idle", "/ws/proj", true]]));
    expect(sessions.rows[0].archived).toBe(true);
  });

  it("filters within the shelf being shown, not across both", () => {
    emit(
      sessionList([
        ["aaa11111", "2026-08-14T09:00:00Z", "idle"],
        ["aaa22222", "2026-08-14T10:00:00Z", "idle", "/ws/proj", true],
      ]),
    );
    setFilter("aaa");
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["aaa11111"]);
    toggleArchivedView();
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["aaa22222"]);
  });

  it("counts the shelf before the filter, so 'none here' reads differently", () => {
    emit(
      sessionList([
        ["live", "2026-08-14T09:00:00Z", "idle"],
        ["filed", "2026-08-14T10:00:00Z", "idle", "/ws/proj", true],
      ]),
    );
    setFilter("zzz");
    expect(visibleRows()).toEqual([]);
    expect(shelfRowCount()).toBe(1);
  });
});

describe("starred rows (TD-3003)", () => {
  it("sorts starred above a newer unstarred row", () => {
    emit(
      sessionList([
        ["older", "2026-08-14T09:00:00Z", "idle", "/ws/proj", false, true],
        ["newer", "2026-08-14T12:00:00Z"],
      ]),
    );
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["older", "newer"]);
  });

  it("filters to starred only when that toggle is on", () => {
    emit(
      sessionList([
        ["pinned", "2026-08-14T09:00:00Z", "idle", "/ws/proj", false, true],
        ["plain", "2026-08-14T12:00:00Z"],
      ]),
    );
    toggleStarredOnly();
    expect(sessions.showStarredOnly).toBe(true);
    expect(visibleRows().map((r) => r.sessionId)).toEqual(["pinned"]);
  });

  it("stars with the daemon's verb", () => {
    emit(sessionList([["s-live", "2026-08-14T09:00:00Z"]]));
    mocks.sent.length = 0;
    setStarred("s-live", true);
    expect(mocks.sent).toEqual([
      { type: "set_session_star", session_id: "s-live", starred: true },
    ]);
  });
});

describe("row lifecycle actions", () => {
  beforeEach(() => {
    emit(
      sessionList([
        ["s-live", "2026-08-14T09:00:00Z", "idle", "/ws/alpha"],
        ["s-filed", "2026-08-14T10:00:00Z", "idle", "/ws/alpha", true],
      ]),
    );
    mocks.sent.length = 0;
  });

  it("archives a row with the daemon's verb", () => {
    setArchived("s-live", true);
    expect(mocks.sent).toEqual([
      { type: "archive_session", session_id: "s-live", archived: true },
    ]);
  });

  it("unarchives with the same verb, flag flipped", () => {
    setArchived("s-filed", false);
    expect(mocks.sent).toEqual([
      { type: "archive_session", session_id: "s-filed", archived: false },
    ]);
  });

  it("never deletes on the first click — Delete arms a confirm and sends nothing", () => {
    requestDelete("s-live");
    expect(sessions.confirmDeleteFor).toBe("s-live");
    expect(mocks.sent).toEqual([]);
  });

  it("sends delete only once confirmed", () => {
    requestDelete("s-live");
    confirmDelete();
    expect(mocks.sent).toEqual([{ type: "delete_session", session_id: "s-live" }]);
    expect(sessions.confirmDeleteFor).toBeNull();
  });

  it("confirming nothing sends nothing", () => {
    expect(confirmDelete()).toBe(false);
    expect(mocks.sent).toEqual([]);
  });

  it("cancelling the confirm sends nothing", () => {
    requestDelete("s-live");
    closeRowMenus();
    expect(sessions.confirmDeleteFor).toBeNull();
    expect(mocks.sent).toEqual([]);
  });

  it("moves a row to a chosen project", () => {
    moveRow("s-live", "/ws/beta");
    expect(mocks.sent).toEqual([
      { type: "move_session", session_id: "s-live", workspace_path: "/ws/beta" },
    ]);
  });

  it("offers every known project except the one the session is already in", () => {
    mocks.workspacesState.entries = [
      { path: "/ws/alpha", lastSeen: "2026-08-14T10:00:00Z" },
      { path: "/ws/beta", lastSeen: "2026-08-13T10:00:00Z" },
    ];
    expect(moveTargets("s-live")).toEqual(["/ws/beta"]);
  });

  it("offers nothing for a row the list doesn't have", () => {
    mocks.workspacesState.entries = [{ path: "/ws/beta", lastSeen: "2026-08-13T10:00:00Z" }];
    expect(moveTargets("no-such-id")).toEqual([]);
  });

  it("opens one row's menu at a time", () => {
    toggleRowMenu("s-live");
    expect(sessions.menuFor).toBe("s-live");
    toggleRowMenu("s-filed");
    expect(sessions.menuFor).toBe("s-filed");
    toggleRowMenu("s-filed");
    expect(sessions.menuFor).toBeNull();
  });
});

describe("refusals", () => {
  // The rail cannot know whether a turn is in flight — the daemon can, and it
  // says so. So the copy shown is the copy sent (AGENTS §6), never a guess.
  const busy = (message: string): DaemonEventUnion => ({
    type: "error",
    seq: 1,
    session_id: "s-live",
    code: "session_busy",
    message,
  });

  beforeEach(() => {
    emit(sessionList([["s-live", "2026-08-14T09:00:00Z", "running"]]));
  });

  it("shows the daemon's refusal verbatim and stands the confirm down", () => {
    requestDelete("s-live");
    confirmDelete();
    emit(busy("This session has a turn in flight, so it can't be deleted yet."));
    expect(sessions.refusal).toBe(
      "This session has a turn in flight, so it can't be deleted yet.",
    );
    expect(sessions.confirmDeleteFor).toBeNull();
  });

  it("closes the move picker on a refusal too", () => {
    requestMove("s-live");
    moveRow("s-live", "/ws/beta");
    emit(busy("This session has a turn in flight, so it can't be moved yet."));
    expect(sessions.moveFor).toBeNull();
    expect(sessions.refusal).toMatch(/moved/);
  });

  it("clears a stale refusal when the next list lands", () => {
    emit(busy("busy"));
    expect(sessions.refusal).not.toBeNull();
    emit(sessionList([["s-live", "2026-08-14T09:00:00Z", "idle"]]));
    expect(sessions.refusal).toBeNull();
  });

  it("ignores error codes that are not lifecycle refusals", () => {
    emit({
      type: "error",
      seq: 1,
      session_id: "s-live",
      code: "session_not_running",
      message: "gone",
    } as DaemonEventUnion);
    expect(sessions.refusal).toBeNull();
  });
});

describe("a moved session keeps its binding", () => {
  // Move is the one action that does not unbind: the session survives, so the
  // pane stays on it and the title bar follows it to the new project.
  it("re-points the title bar at the new workspace", () => {
    mocks.chatState.sessionId = "s-live";
    emit(sessionList([["s-live", "2026-08-14T09:00:00Z", "idle", "/ws/alpha"]]));
    mocks.retargets.length = 0;
    emit(sessionList([["s-live", "2026-08-14T09:30:00Z", "idle", "/ws/beta"]]));
    expect(mocks.retargets.at(-1)).toEqual({ id: "s-live", workspacePath: "/ws/beta" });
  });

  it("re-points nothing when no session is bound", () => {
    mocks.chatState.sessionId = null;
    emit(sessionList([["s-live", "2026-08-14T09:00:00Z", "idle", "/ws/beta"]]));
    expect(mocks.retargets).toEqual([]);
  });
});
