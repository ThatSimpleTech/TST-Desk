// The auto-bind policy table (TD-1711, TD-1714, TD-1715).
//
// chat-store.test.ts drives these rules through the store, which is the right
// place to prove the pane behaves. These drive them directly, because the
// rules are the part defects keep landing in: a terminal session adopted after
// a restart, an archived one adopted back, a summary's session-liveness read
// as turn evidence. One call, one answer, no transport in the way.

import { describe, it, expect } from "vitest";
import { applyBind, chooseBoundSession, type BindContext } from "./session-binding";
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
});
