// The auto-bind policy table (TD-1711, TD-1714, TD-1715).
//
// chat-store.test.ts drives these rules through the store, which is the right
// place to prove the pane behaves. These drive them directly, because the
// rules are the part defects keep landing in: a terminal session adopted after
// a restart, an archived one adopted back, a summary's session-liveness read
// as turn evidence. One call, one answer, no transport in the way.

import { describe, it, expect } from "vitest";
import { applyBind, chooseBoundSession, type BindContext } from "./session-binding";
import { trayRunningCount } from "./coworker-indicator";
import { createChatState } from "./chat-store";
import type { SessionSummary } from "./protocol";

type Row = {
  id: string;
  updated: string;
  state?: SessionSummary["state"];
  archived?: boolean;
};

function summaries(rows: Row[]): SessionSummary[] {
  return rows.map((row) => ({
    session_id: row.id,
    workspace_path: "/workspace",
    state: row.state ?? "idle",
    created_at: row.updated,
    updated_at: row.updated,
    event_count: 0,
    archived: row.archived ?? false,
    starred: false,
  }));
}

describe("choosing a session to bind", () => {
  it("unbinds when the daemon lists nothing", () => {
    expect(chooseBoundSession([], null)).toEqual({ action: "unbind" });
    expect(chooseBoundSession([], "gone")).toEqual({ action: "unbind" });
  });

  it("keeps the current session while it is listed and unarchived", () => {
    const choice = chooseBoundSession(
      summaries([
        { id: "bound", updated: "2026-08-14T09:00:00Z" },
        { id: "newer", updated: "2026-08-14T11:00:00Z" },
      ]),
      "bound",
    );
    expect(choice).toEqual({ action: "keep", turnState: "idle" });
  });

  it("keeps the current session after its turn completes", () => {
    // Terminal only bars *auto-bind*. The conversation the user is reading
    // stays on screen when its last turn ends.
    const choice = chooseBoundSession(
      summaries([{ id: "bound", updated: "2026-08-14T09:00:00Z", state: "complete" }]),
      "bound",
    );
    expect(choice).toEqual({ action: "keep", turnState: "complete" });
  });

  it("rebinds rather than keeps when the current session is archived (TD-1715)", () => {
    const choice = chooseBoundSession(
      summaries([
        { id: "bound", updated: "2026-08-14T11:00:00Z", archived: true },
        { id: "other", updated: "2026-08-14T09:00:00Z" },
      ]),
      "bound",
    );
    expect(choice).toEqual({ action: "bind", sessionId: "other", turnState: "idle" });
  });

  it("keeps an archived session the pane was opened on from the archived shelf (TD-5201)", () => {
    const list = summaries([
      { id: "filed", updated: "2026-08-14T09:00:00Z", archived: true },
      { id: "newer", updated: "2026-08-14T11:00:00Z" },
    ]);
    expect(chooseBoundSession(list, "filed", null, null, true)).toEqual({
      action: "keep",
      turnState: "idle",
    });
  });

  it("still moves off the bound session in the list that archives it (TD-5201)", () => {
    const after = summaries([
      { id: "bound", updated: "2026-08-14T11:00:00Z", archived: true },
      { id: "other", updated: "2026-08-14T08:00:00Z" },
    ]);
    expect(chooseBoundSession(after, "bound", null, null, false)).toEqual({
      action: "bind",
      sessionId: "other",
      turnState: "idle",
    });
  });

  it("unbinds rather than binding a corpse when every session is terminal (TD-1711)", () => {
    const choice = chooseBoundSession(
      summaries([
        { id: "a", updated: "2026-08-14T09:00:00Z", state: "interrupted" },
        { id: "b", updated: "2026-08-14T10:00:00Z", state: "complete" },
        { id: "c", updated: "2026-08-14T11:00:00Z", state: "failed" },
        { id: "d", updated: "2026-08-14T12:00:00Z", state: "cancelled" },
      ]),
      null,
    );
    expect(choice).toEqual({ action: "unbind" });
  });

  it("binds the most recently updated live session", () => {
    const choice = chooseBoundSession(
      summaries([
        { id: "old", updated: "2026-08-13T09:00:00Z" },
        { id: "newest", updated: "2026-08-14T12:00:00Z" },
        { id: "middle", updated: "2026-08-14T10:00:00Z" },
        { id: "newest-but-dead", updated: "2026-08-15T09:00:00Z", state: "complete" },
      ]),
      null,
    );
    expect(choice).toEqual({ action: "bind", sessionId: "newest", turnState: "idle" });
  });

  it("never stamps a turn state from a summary that says running (TD-1714)", () => {
    // "running" on a summary means the session's loop is alive — it is set
    // once at open and spans the session's life. Taking it as turn evidence
    // locks the composer behind the stop morph on every refresh.
    const choice = chooseBoundSession(
      summaries([{ id: "bound", updated: "2026-08-14T09:00:00Z", state: "running" }]),
      "bound",
    );
    expect(choice).toEqual({ action: "keep", turnState: null });
  });

  it("binds a window-scoped viewer to its session id (TD-4703)", () => {
    const list = summaries([
      { id: "newest", updated: "2026-08-14T10:00:00Z", state: "idle" },
      { id: "bound", updated: "2026-08-14T09:00:00Z", state: "idle" },
    ]);
    expect(chooseBoundSession(list, null, "bound")).toEqual({
      action: "bind",
      sessionId: "bound",
      turnState: "idle",
    });
    expect(chooseBoundSession(list, "bound", "bound")).toEqual({
      action: "keep",
      turnState: "idle",
    });
    expect(chooseBoundSession(list, "bound", "other")).toEqual({
      action: "unbind",
    });
  });

  it("keeps a window-scoped viewer on its chat once archived or ended (TD-5201)", () => {
    // "Open in new window" is on every row, the archived shelf's included.
    for (const row of [
      { archived: true },
      { state: "complete" as const },
      { state: "cancelled" as const },
      { state: "interrupted" as const },
      { state: "failed" as const },
    ]) {
      const list = summaries([
        { id: "newest", updated: "2026-08-14T10:00:00Z" },
        { id: "bound", updated: "2026-08-14T09:00:00Z", ...row },
      ]);
      const turnState = row.state ?? "idle";
      expect(chooseBoundSession(list, null, "bound")).toEqual({
        action: "bind",
        sessionId: "bound",
        turnState,
      });
      expect(chooseBoundSession(list, "bound", "bound")).toEqual({ action: "keep", turnState });
    }
  });
});

describe("tray running-count", () => {
  it("counts running and awaiting_approval only", () => {
    expect(trayRunningCount(["idle", "running", "awaiting_approval", "complete"])).toBe(2);
  });
});

function bindContext() {
  const attached: string[] = [];
  const detached: string[] = [];
  const bound: (string | null)[] = [];
  const calls: string[] = [];
  const ctx: BindContext = {
    queue: {
      clear: () => {
        calls.push("queue.clear");
      },
    },
    wait: {
      end: () => {
        calls.push("wait.end");
      },
    },
    deps: {
      attach: (id) => {
        attached.push(id);
        calls.push("attach");
      },
      detach: (id) => {
        detached.push(id);
        calls.push("detach");
      },
      onBind: (id) => {
        bound.push(id);
        calls.push("onBind");
      },
    },
  };
  return { ctx, attached, detached, bound, calls };
}

describe("applying a bind", () => {
  it("tears the old session down before standing the new one up", () => {
    const { ctx, attached, detached, bound, calls } = bindContext();
    const state = createChatState();
    applyBind(state, ctx, "first", "idle");
    applyBind(state, ctx, "second", "idle");

    expect(state.sessionId).toBe("second");
    expect(detached).toEqual(["first"]);
    expect(attached).toEqual(["first", "second"]);
    // The activity lane is pointed at the session before the attach fetches
    // the replay that fills it (TD-1009).
    expect(bound).toEqual(["first", "second"]);
    expect(calls.indexOf("onBind")).toBeLessThan(calls.indexOf("attach"));
  });

  it("clears the pane, the queue and the wait", () => {
    const { ctx, calls } = bindContext();
    const state = createChatState();
    state.messages = [{ id: "m1", role: "user", text: "hi", complete: true, at: 0 }];
    state.lastTurnDuration = 4;
    applyBind(state, ctx, "fresh", "idle");

    expect(state.messages).toEqual([]);
    expect(state.lastTurnDuration).toBeNull();
    expect(calls).toContain("queue.clear");
    expect(calls).toContain("wait.end");
  });

  it("refuses a summary's running as turn state (TD-1714)", () => {
    const { ctx } = bindContext();
    const state = createChatState();
    applyBind(state, ctx, "live", "running");
    expect(state.sessionId).toBe("live");
    expect(state.turnState).toBeNull();
  });

  it("does nothing when the pane is already on that session", () => {
    const { ctx, attached, detached, calls } = bindContext();
    const state = createChatState();
    applyBind(state, ctx, "same", "idle");
    state.messages = [{ id: "m1", role: "assistant", text: "kept", complete: true, at: 0 }];
    applyBind(state, ctx, "same", "awaiting_approval");

    expect(state.messages).toHaveLength(1);
    expect(state.turnState).toBe("idle");
    expect(attached).toEqual(["same"]);
    expect(detached).toEqual([]);
    expect(calls.filter((c) => c === "onBind")).toHaveLength(1);
  });

  it("unbinds to nothing without attaching", () => {
    const { ctx, attached, detached, bound } = bindContext();
    const state = createChatState();
    applyBind(state, ctx, "only", "idle");
    applyBind(state, ctx, null, null);

    expect(state.sessionId).toBeNull();
    expect(detached).toEqual(["only"]);
    expect(attached).toEqual(["only"]);
    expect(bound).toEqual(["only", null]);
  });

  it("fires onUnbind when the pane leaves a session (TD-1902)", () => {
    const { ctx } = bindContext();
    const unbound: string[] = [];
    ctx.onUnbind = () => unbound.push("cleared");
    const state = createChatState();
    applyBind(state, ctx, "first", "idle");
    applyBind(state, ctx, "second", "idle");
    expect(unbound).toEqual(["cleared", "cleared"]);
    applyBind(state, ctx, "second", "idle");
    expect(unbound).toEqual(["cleared", "cleared"]);
  });
});

describe("sessions on another computer (TD-5015)", () => {
  const REMOTE = "3f2a9c1e-7b4d-4e8a-9f01-2c3d4e5f6a7b:mch_0123456789ab";

  it("never auto-binds another computer's session, even the newest", () => {
    const choice = chooseBoundSession(
      summaries([
        { id: "own", updated: "2026-08-14T09:00:00Z" },
        { id: REMOTE, updated: "2026-08-14T11:00:00Z" },
      ]),
      null,
    );
    expect(choice).toEqual({ action: "bind", sessionId: "own", turnState: "idle" });
  });

  it("stays unbound when only another computer's sessions are live", () => {
    expect(chooseBoundSession(summaries([{ id: REMOTE, updated: "2026-08-14T11:00:00Z" }]), null)).toEqual({
      action: "unbind",
    });
  });

  it("keeps a remote session the pane already follows", () => {
    const choice = chooseBoundSession(summaries([{ id: REMOTE, updated: "2026-08-14T11:00:00Z" }]), REMOTE);
    expect(choice).toEqual({ action: "keep", turnState: "idle" });
  });

  it("keeps a held remote session the hub's cached list doesn't name yet", () => {
    // A new session on the paired computer: its first session_state arrived,
    // but the hub's cache refreshes a second later.
    const fresh = "newraw:mch_0123456789ab";
    const list = summaries([{ id: "own", updated: "2026-08-14T12:00:00Z" }]);
    expect(chooseBoundSession(list, fresh, null, fresh)).toEqual({ action: "keep", turnState: null });
  });

  it("rebinds off a remote session once nothing holds it (deleted there, or never held)", () => {
    const list = summaries([{ id: "own", updated: "2026-08-14T12:00:00Z" }]);
    expect(chooseBoundSession(list, REMOTE)).toEqual({ action: "bind", sessionId: "own", turnState: "idle" });
    // A hold only covers the id it names, and never an own session.
    expect(chooseBoundSession(list, REMOTE, null, "other:mch_0123456789ab")).toEqual({
      action: "bind",
      sessionId: "own",
      turnState: "idle",
    });
    expect(chooseBoundSession(list, "gone", null, "gone")).toEqual({
      action: "bind",
      sessionId: "own",
      turnState: "idle",
    });
  });

  it("lets a window bind target a bridged id", () => {
    const choice = chooseBoundSession(
      summaries([
        { id: "own", updated: "2026-08-14T12:00:00Z" },
        { id: REMOTE, updated: "2026-08-14T11:00:00Z" },
      ]),
      null,
      REMOTE,
    );
    expect(choice).toEqual({ action: "bind", sessionId: REMOTE, turnState: "idle" });
  });
});
