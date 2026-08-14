// Chat store behavior tests (TD-1004). Node environment, no DOM: the store
// is pure logic with injected transport deps, so these tests drive it exactly
// the way the connection fan-out and the components do.

import { describe, it, expect } from "vitest";
import {
  canSend,
  createChatState,
  createChatStore,
  formatTurnDuration,
  shouldSubmit,
  showCancel,
  type ChatDeps,
} from "./chat-store";
import type { ClientMessageUnion, DaemonEventUnion, SessionState } from "./protocol";

function fakeDeps(sendResult = true) {
  const sent: ClientMessageUnion[] = [];
  const attached: string[] = [];
  const detached: string[] = [];
  const deps: ChatDeps = {
    send: (msg) => {
      sent.push(msg);
      return sendResult;
    },
    attach: (id) => {
      attached.push(id);
    },
    detach: (id) => {
      detached.push(id);
    },
  };
  return { deps, sent, attached, detached };
}

function delta(sessionId: string, text: string): DaemonEventUnion {
  return { type: "assistant_delta", session_id: sessionId, delta: text, seq: 1 };
}

function turnComplete(sessionId: string, duration = 0): DaemonEventUnion {
  return { type: "turn_complete", session_id: sessionId, tokens: 1, cost: 0, tier: "worker", duration, failed: false, error_code: null, seq: 2 };
}

function sessionState(sessionId: string, state: SessionState["state"]): DaemonEventUnion {
  return { type: "session_state", session_id: sessionId, state, seq: 3 };
}

type Summary = { id: string; updated: string; state?: SessionSummaryState };
type SessionSummaryState = "idle" | "running" | "awaiting_approval" | "complete" | "failed" | "cancelled" | "interrupted";

function sessionList(summaries: Summary[]): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 4,
    sessions: summaries.map((s) => ({
      session_id: s.id,
      workspace_path: "/workspace",
      state: s.state ?? "idle",
      created_at: s.updated,
      updated_at: s.updated,
      event_count: 0,
    })),
  };
}

function boundStore(sendResult = true) {
  const { deps, sent, attached, detached } = fakeDeps(sendResult);
  const state = createChatState();
  const store = createChatStore(deps, state);
  store.applyEvent(sessionList([{ id: "s1", updated: "2026-08-14T10:00:00Z", state: "idle" }]));
  return { store, state, sent, attached, detached };
}

describe("session binding", () => {
  it("binds the most recently updated session and attaches", () => {
    const { deps, attached } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    store.applyEvent(
      sessionList([
        { id: "old", updated: "2026-08-13T09:00:00Z" },
        { id: "new", updated: "2026-08-14T10:00:00Z" },
      ]),
    );
    expect(state.sessionId).toBe("new");
    expect(attached).toEqual(["new"]);
  });

  it("keeps the current session while the daemon still lists it", () => {
    const { store, state, attached } = boundStore();
    store.applyEvent(sessionList([{ id: "s1", updated: "2026-08-14T11:00:00Z" }, { id: "s2", updated: "2026-08-14T12:00:00Z" }]));
    expect(state.sessionId).toBe("s1");
    expect(attached).toEqual(["s1"]);
  });

  it("switches when the current session disappears", () => {
    const { store, state, attached, detached } = boundStore();
    store.applyEvent(delta("s1", "hello"));
    store.applyEvent(sessionList([{ id: "s2", updated: "2026-08-14T12:00:00Z", state: "running" }]));
    expect(state.sessionId).toBe("s2");
    expect(state.turnState).toBe("running");
    expect(state.messages).toEqual([]);
    expect(detached).toEqual(["s1"]);
    expect(attached).toEqual(["s1", "s2"]);
  });

  it("clears the session when the list empties", () => {
    const { store, state, detached } = boundStore();
    store.applyEvent(sessionList([]));
    expect(state.sessionId).toBeNull();
    expect(state.turnState).toBeNull();
    expect(detached).toEqual(["s1"]);
  });

  it("rail selection detaches the old session and attaches the chosen one (TD-1701)", () => {
    const { store, state, attached, detached } = boundStore();
    store.applyEvent(delta("s1", "in flight"));
    store.selectSession("s2", "interrupted");
    expect(state.sessionId).toBe("s2");
    expect(state.turnState).toBe("interrupted");
    expect(state.messages).toEqual([]);
    expect(state.awaitingFirstToken).toBe(false);
    expect(detached).toEqual(["s1"]);
    expect(attached).toEqual(["s1", "s2"]); // s1 bound at start, s2 on select
  });

  it("rail selection of the attached session is a no-op", () => {
    const { store, state, attached, detached } = boundStore();
    store.applyEvent(delta("s1", "keep me"));
    store.selectSession("s1", "idle");
    expect(state.messages).toHaveLength(1);
    expect(attached).toEqual(["s1"]);
    expect(detached).toEqual([]);
  });

  it("rail selection of a running session shows the working shimmer", () => {
    const { store, state } = boundStore();
    store.selectSession("s2", "running");
    expect(state.awaitingFirstToken).toBe(true);
  });
});

describe("streaming assistant output", () => {
  it("first delta opens a message, later deltas append to the same object", () => {
    const { store, state } = boundStore();
    store.applyEvent(delta("s1", "Hello"));
    expect(state.messages).toHaveLength(1);
    const message = state.messages[0];
    expect(message.role).toBe("assistant");
    expect(message.complete).toBe(false);
    store.applyEvent(delta("s1", ", world"));
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toBe(message); // identity stable — no row re-mount
    expect(state.messages[0].text).toBe("Hello, world");
  });

  it("turn_complete seals the message; the next delta opens a new one", () => {
    const { store, state } = boundStore();
    store.applyEvent(delta("s1", "first"));
    store.applyEvent(turnComplete("s1"));
    expect(state.messages[0].complete).toBe(true);
    store.applyEvent(delta("s1", "second"));
    expect(state.messages).toHaveLength(2);
    expect(state.messages[1].text).toBe("second");
    expect(state.messages[1].complete).toBe(false);
  });

  it("rebuilds full history from an attach replay through the same reducer", () => {
    const { store, state } = boundStore();
    // What the daemon replays on attach: the whole event log in order.
    const replay: DaemonEventUnion[] = [
      delta("s1", "Answer one."),
      turnComplete("s1"),
      delta("s1", "Answer"),
      delta("s1", " two."),
      turnComplete("s1"),
      sessionState("s1", "idle"),
    ];
    for (const event of replay) store.applyEvent(event);
    expect(state.messages.map((m) => m.text)).toEqual(["Answer one.", "Answer two."]);
    expect(state.messages.every((m) => m.complete)).toBe(true);
    expect(state.turnState).toBe("idle");
  });

  it("seals an in-flight message on terminal session states", () => {
    const { store, state } = boundStore();
    store.applyEvent(delta("s1", "partial"));
    store.applyEvent(sessionState("s1", "cancelled"));
    expect(state.messages[0].complete).toBe(true);
    expect(state.turnState).toBe("cancelled");
  });

  it("ignores events for sessions that are not bound", () => {
    const { store, state } = boundStore();
    store.applyEvent(delta("elsewhere", "noise"));
    store.applyEvent(sessionState("elsewhere", "running"));
    expect(state.messages).toEqual([]);
    expect(state.turnState).toBe("idle");
  });
});

describe("sending and cancelling", () => {
  it("sendUserMessage echoes locally and puts user_message on the wire", () => {
    const { store, state, sent } = boundStore();
    const ok = store.sendUserMessage("  fix the tests  ");
    expect(ok).toBe(true);
    expect(sent).toEqual([{ type: "user_message", session_id: "s1", content: "fix the tests" }]);
    expect(state.messages[0]).toMatchObject({ role: "user", text: "fix the tests", complete: true });
  });

  it("sendUserMessage refuses empty content and missing session", () => {
    const { store, sent } = boundStore();
    expect(store.sendUserMessage("   ")).toBe(false);
    const { deps, sent: sent2 } = fakeDeps();
    const loose = createChatStore(deps, createChatState());
    expect(loose.sendUserMessage("hi")).toBe(false);
    expect(sent).toEqual([]);
    expect(sent2).toEqual([]);
  });

  it("sendUserMessage does not echo when the wire send fails", () => {
    const { store, state } = boundStore(false);
    expect(store.sendUserMessage("hello")).toBe(false);
    expect(state.messages).toEqual([]);
  });

  it("cancelTurn sends cancel with the active session id", () => {
    const { store, sent } = boundStore();
    expect(store.cancelTurn()).toBe(true);
    expect(sent).toEqual([{ type: "cancel", session_id: "s1" }]);
  });

  it("cancelTurn refuses without a session", () => {
    const { deps, sent } = fakeDeps();
    const store = createChatStore(deps, createChatState());
    expect(store.cancelTurn()).toBe(false);
    expect(sent).toEqual([]);
  });

  it("refreshSessions asks the daemon for the list", () => {
    const { store, sent } = boundStore();
    store.refreshSessions();
    expect(sent).toEqual([{ type: "list_sessions" }]);
  });

  it("dispose detaches and clears", () => {
    const { store, state, detached } = boundStore();
    store.applyEvent(delta("s1", "text"));
    store.dispose();
    expect(detached).toEqual(["s1"]);
    expect(state).toMatchObject({ sessionId: null, turnState: null, messages: [] });
  });
});

describe("retry (TD-1606)", () => {
  it("resends the last user message verbatim as a new user_message", () => {
    const { store, state, sent } = boundStore();
    store.sendUserMessage("first prompt");
    store.applyEvent(delta("s1", "answer one"));
    store.applyEvent(turnComplete("s1"));
    store.sendUserMessage("second prompt");
    store.applyEvent(delta("s1", "answer two"));
    store.applyEvent(turnComplete("s1"));
    expect(store.retryLastUserMessage()).toBe(true);
    expect(sent.filter((m) => m.type === "user_message")).toEqual([
      { type: "user_message", session_id: "s1", content: "first prompt" },
      { type: "user_message", session_id: "s1", content: "second prompt" },
      { type: "user_message", session_id: "s1", content: "second prompt" },
    ]);
    // The resend appends a new row: the protocol has no edit/fork, so the
    // duplication is the honest record of the retry.
    expect(state.messages.filter((m) => m.role === "user").map((m) => m.text)).toEqual([
      "first prompt",
      "second prompt",
      "second prompt",
    ]);
  });

  it("refuses while a turn is running or awaiting approval", () => {
    const { store, state, sent } = boundStore();
    store.sendUserMessage("do the thing");
    state.turnState = "running";
    expect(store.retryLastUserMessage()).toBe(false);
    state.turnState = "awaiting_approval";
    expect(store.retryLastUserMessage()).toBe(false);
    expect(sent.filter((m) => m.type === "user_message")).toHaveLength(1);
  });

  it("refuses when no user message exists or no session is bound", () => {
    const { store, sent } = boundStore();
    expect(store.retryLastUserMessage()).toBe(false);
    const { deps, sent: sent2 } = fakeDeps();
    const loose = createChatStore(deps, createChatState());
    expect(loose.retryLastUserMessage()).toBe(false);
    expect(sent).toEqual([]);
    expect(sent2).toEqual([]);
  });

  it("stamps every message with a client-side seen-at time", () => {
    const { store, state } = boundStore();
    const before = Date.now();
    store.sendUserMessage("hello");
    store.applyEvent(delta("s1", "hi"));
    const after = Date.now();
    for (const m of state.messages) {
      expect(m.at).toBeGreaterThanOrEqual(before);
      expect(m.at).toBeLessThanOrEqual(after);
    }
  });
});

describe("turn status (TD-1607)", () => {
  it("awaits a first token between send and the first delta", () => {
    const { store, state } = boundStore();
    expect(state.awaitingFirstToken).toBe(false);
    store.sendUserMessage("go");
    expect(state.awaitingFirstToken).toBe(true);
    store.applyEvent(delta("s1", "on it"));
    expect(state.awaitingFirstToken).toBe(false);
  });

  it("a failed wire send does not raise the working shimmer", () => {
    const { store, state } = boundStore(false);
    store.sendUserMessage("go");
    expect(state.awaitingFirstToken).toBe(false);
  });

  it("turn_complete stamps the daemon-measured duration and clears the flag", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("go");
    store.applyEvent(delta("s1", "done"));
    store.applyEvent(turnComplete("s1", 42.4));
    expect(state.awaitingFirstToken).toBe(false);
    expect(state.lastTurnDuration).toBe(42.4);
  });

  it("the next send clears the previous duration line", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("first");
    store.applyEvent(delta("s1", "ok"));
    store.applyEvent(turnComplete("s1", 10));
    store.sendUserMessage("second");
    expect(state.lastTurnDuration).toBeNull();
    expect(state.awaitingFirstToken).toBe(true);
  });

  it("a terminal session state clears the shimmer even with no deltas", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("go");
    store.applyEvent(sessionState("s1", "cancelled"));
    expect(state.awaitingFirstToken).toBe(false);
  });

  it("attaching to a running session shows the shimmer; a replayed running state does not resurrect it mid-stream", () => {
    const { store, state } = boundStore();
    store.applyEvent(sessionList([{ id: "s2", updated: "2026-08-14T11:00:00Z", state: "running" }]));
    expect(state.sessionId).toBe("s2");
    expect(state.awaitingFirstToken).toBe(true);
    // Replay continues: a delta lands, then the trailing current-state event.
    store.applyEvent(delta("s2", "partial answer"));
    store.applyEvent(sessionState("s2", "running"));
    expect(state.awaitingFirstToken).toBe(false);
  });

  it("formatTurnDuration never reads 0s and rolls over into minutes", () => {
    expect(formatTurnDuration(0.2)).toBe("1s");
    expect(formatTurnDuration(42.4)).toBe("42s");
    expect(formatTurnDuration(59.6)).toBe("1m 0s");
    expect(formatTurnDuration(90)).toBe("1m 30s");
  });
});

describe("composer and control predicates", () => {
  it("Enter submits, Shift+Enter newlines, other keys do nothing", () => {
    expect(shouldSubmit("Enter", false)).toBe(true);
    expect(shouldSubmit("Enter", true)).toBe(false);
    expect(shouldSubmit("a", false)).toBe(false);
  });

  it("cancel shows whenever a turn is live", () => {
    expect(showCancel("running")).toBe(true);
    expect(showCancel("awaiting_approval")).toBe(true);
    expect(showCancel("idle")).toBe(false);
    expect(showCancel("complete")).toBe(false);
    expect(showCancel(null)).toBe(false);
  });

  it("composer enables only with a session and a live socket", () => {
    expect(canSend("s1", "connected")).toBe(true);
    expect(canSend(null, "connected")).toBe(false);
    expect(canSend("s1", "reconnecting")).toBe(false);
    expect(canSend("s1", "disconnected")).toBe(false);
  });
});
