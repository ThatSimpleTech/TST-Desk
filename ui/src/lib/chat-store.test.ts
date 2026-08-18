// Chat store behavior tests (TD-1004). Node environment, no DOM: the store
// is pure logic with injected transport deps, so these tests drive it exactly
// the way the connection fan-out and the components do.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  canSend,
  createChatState,
  createChatStore,
  formatTurnDuration,
  shouldSubmit,
  showCancel,
  STALL_TIMEOUT_MS,
  type ChatDeps,
} from "./chat-store";
import { showQueue } from "./chat-queue";
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

type Summary = { id: string; updated: string; state?: SessionSummaryState; archived?: boolean };
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
      archived: s.archived ?? false,
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
    // TD-1714: the summary's "running" is session-liveness — no turn yet.
    expect(state.turnState).toBeNull();
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

  it("rail selection of a live session does not fabricate a turn (TD-1714)", () => {
    const { store, state } = boundStore();
    store.selectSession("s2", "running");
    // "running" is the daemon's session-alive state; with no turn evidence
    // the composer must offer send, not stop.
    expect(state.turnState).toBeNull();
    expect(state.awaitingFirstToken).toBe(false);
    expect(showCancel(state.turnState)).toBe(false);
  });

  it("the composer can send immediately after auto-binding a live session (TD-1714)", () => {
    const { deps, sent } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    // The 2026-08-14 lockout: auto-bind to a live session, then the attach
    // replay's open-time session_state — both said "running", and the
    // composer morphed send → stop forever.
    store.applyEvent(sessionList([{ id: "s2", updated: "2026-08-14T12:00:00Z", state: "running" }]));
    expect(state.sessionId).toBe("s2");
    store.applyEvent(sessionState("s2", "running"));
    expect(showCancel(state.turnState)).toBe(false);
    expect(state.awaitingFirstToken).toBe(false);
    expect(store.sendUserMessage("hello")).toBe(true);
    expect(sent).toContainEqual({ type: "user_message", session_id: "s2", content: "hello" });
    expect(state.awaitingFirstToken).toBe(true);
    store.dispose();
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

  it("an attach replay derives the turn from evidence, not the open-time running state (TD-1714)", () => {
    const { store, state } = boundStore();
    store.applyEvent(sessionList([{ id: "s2", updated: "2026-08-14T11:00:00Z", state: "running" }]));
    expect(state.sessionId).toBe("s2");
    expect(state.turnState).toBeNull();
    expect(state.awaitingFirstToken).toBe(false);
    // The replay's first event is the open-time session_state: the session
    // was alive then — still no turn evidence.
    store.applyEvent(sessionState("s2", "running"));
    expect(state.turnState).toBeNull();
    expect(state.awaitingFirstToken).toBe(false);
    // Replayed deltas are turn evidence: the turn reads live mid-stream…
    store.applyEvent(delta("s2", "partial answer"));
    expect(state.turnState).toBe("running");
    // …a replayed running state mid-stream keeps it (deltas corroborate)…
    store.applyEvent(sessionState("s2", "running"));
    expect(state.turnState).toBe("running");
    expect(state.awaitingFirstToken).toBe(false);
    // …and the replayed completion stands the turn down.
    store.applyEvent(turnComplete("s2"));
    expect(state.turnState).toBeNull();
    expect(showCancel(state.turnState)).toBe(false);
  });

  it("a session_list refresh never stamps the alive-state lie over the turn (TD-1714)", () => {
    const { store, state } = boundStore();
    // At rest: a summary saying "running" must not raise a phantom turn.
    store.applyEvent(sessionList([{ id: "s1", updated: "2026-08-14T11:00:00Z", state: "running" }]));
    expect(state.sessionId).toBe("s1");
    expect(state.turnState).toBe("idle");
    // Mid-turn: the same refresh must not stand it down either — local
    // evidence owns "running".
    store.sendUserMessage("go");
    store.applyEvent(delta("s1", "working"));
    expect(state.turnState).toBe("running");
    store.applyEvent(sessionList([{ id: "s1", updated: "2026-08-14T11:05:00Z", state: "running" }]));
    expect(state.turnState).toBe("running");
  });

  it("an approval resolution keeps the turn live through the running state", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("run the risky thing");
    store.applyEvent(sessionState("s1", "awaiting_approval"));
    expect(state.turnState).toBe("awaiting_approval");
    store.applyEvent(sessionState("s1", "running"));
    expect(state.turnState).toBe("running");
  });

  it("formatTurnDuration never reads 0s and rolls over into minutes", () => {
    expect(formatTurnDuration(0.2)).toBe("1s");
    expect(formatTurnDuration(42.4)).toBe("42s");
    expect(formatTurnDuration(59.6)).toBe("1m 0s");
    expect(formatTurnDuration(90)).toBe("1m 30s");
  });
});

describe("session liveness honesty (TD-1711)", () => {
  const TERMINAL: SessionSummaryState[] = ["complete", "failed", "cancelled", "interrupted"];

  function notRunningError(sessionId: string | null): DaemonEventUnion {
    return {
      type: "error",
      code: "session_not_running",
      message: "This session is cancelled and can no longer run turns; the message was not delivered. Start a new session and resend it.",
      session_id: sessionId,
      seq: 9,
    } as DaemonEventUnion;
  }

  it.each(TERMINAL)("auto-bind skips a %s session even when it is the newest", (state) => {
    const { deps, attached } = fakeDeps();
    const store = createChatStore(deps, createChatState());
    store.applyEvent(
      sessionList([
        { id: "older-live", updated: "2026-08-14T09:00:00Z", state: "idle" },
        { id: "newest-dead", updated: "2026-08-14T11:00:00Z", state },
      ]),
    );
    expect(store.state.sessionId).toBe("older-live");
    expect(attached).toEqual(["older-live"]);
  });

  it("stays unbound when every listed session is terminal", () => {
    const { deps, attached } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    store.applyEvent(
      sessionList([
        { id: "tomb-a", updated: "2026-08-14T09:00:00Z", state: "interrupted" },
        { id: "tomb-b", updated: "2026-08-14T11:00:00Z", state: "cancelled" },
      ]),
    );
    expect(state.sessionId).toBeNull();
    expect(state.turnState).toBeNull();
    expect(attached).toEqual([]);
  });

  it("keeps the current session even when it has gone terminal", () => {
    const { store, state } = boundStore();
    state.turnState = "complete";
    store.applyEvent(sessionList([{ id: "s1", updated: "2026-08-14T11:00:00Z", state: "complete" }]));
    // The user is looking at it — don't yank the pane; the daemon rejects
    // any further sends instead.
    expect(state.sessionId).toBe("s1");
    expect(state.turnState).toBe("complete");
  });

  it("a session_not_running error drops the waiting shimmer for the bound session", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("hello");
    expect(state.awaitingFirstToken).toBe(true);
    store.applyEvent(notRunningError("s1"));
    expect(state.awaitingFirstToken).toBe(false);
  });

  it("ignores the refusal addressed at another session", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("hello");
    store.applyEvent(notRunningError("elsewhere"));
    expect(state.awaitingFirstToken).toBe(true);
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

describe("first-token watchdog (TD-1713)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("stalls the Working state after 25s with no first token, still offering cancel", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("hello");
    expect(state.awaitingFirstToken).toBe(true);
    expect(state.turnStalled).toBe(false);

    vi.advanceTimersByTime(STALL_TIMEOUT_MS - 1);
    expect(state.turnStalled).toBe(false);

    vi.advanceTimersByTime(1);
    expect(state.turnStalled).toBe(true);
    // The wait isn't over — honesty changes the copy, not the affordances.
    expect(state.awaitingFirstToken).toBe(true);
    store.dispose();
  });

  it("recovers on the first delta: stalled copy drops and streaming proceeds", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("hello");
    vi.advanceTimersByTime(STALL_TIMEOUT_MS);
    expect(state.turnStalled).toBe(true);

    store.applyEvent(delta("s1", "sorry, cold start"));
    expect(state.turnStalled).toBe(false);
    expect(state.awaitingFirstToken).toBe(false);
    expect(state.messages.at(-1)?.text).toBe("sorry, cold start");
    store.dispose();
  });

  it("never fires once the turn resolves first (complete, cancel, or refusal)", () => {
    // turn_complete clears
    const a = boundStore();
    a.store.sendUserMessage("hello");
    a.store.applyEvent(turnComplete("s1"));
    vi.advanceTimersByTime(STALL_TIMEOUT_MS * 2);
    expect(a.state.turnStalled).toBe(false);

    // terminal session_state clears
    const b = boundStore();
    b.store.sendUserMessage("hello");
    b.store.applyEvent(sessionState("s1", "failed"));
    vi.advanceTimersByTime(STALL_TIMEOUT_MS * 2);
    expect(b.state.turnStalled).toBe(false);

    // session_not_running refusal clears
    const c = boundStore();
    c.store.sendUserMessage("hello");
    c.store.applyEvent({
      type: "error",
      code: "session_not_running",
      message: "dead",
      session_id: "s1",
      seq: 9,
    } as unknown as DaemonEventUnion);
    vi.advanceTimersByTime(STALL_TIMEOUT_MS * 2);
    expect(c.state.turnStalled).toBe(false);

    a.store.dispose();
    b.store.dispose();
    c.store.dispose();
  });

  it("switching sessions or disposing disarms the old session's watchdog", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("hello");
    store.selectSession("s2", "idle");
    expect(state.turnStalled).toBe(false);
    vi.advanceTimersByTime(STALL_TIMEOUT_MS * 2);
    expect(state.turnStalled).toBe(false);
    store.dispose();
  });

  it("a stalled wait cancels locally the moment the user hits cancel", () => {
    const { store, state, sent } = boundStore();
    store.sendUserMessage("hello");
    vi.advanceTimersByTime(STALL_TIMEOUT_MS);
    expect(state.turnStalled).toBe(true);

    expect(store.cancelTurn()).toBe(true);
    expect(sent).toContainEqual({ type: "cancel", session_id: "s1" });
    expect(state.turnStalled).toBe(false);
    expect(state.awaitingFirstToken).toBe(false);
    store.dispose();
  });

  it("does not arm when attaching to a live session with no turn evidence (TD-1714)", () => {
    const { store, state } = boundStore();
    store.selectSession("s2", "running");
    expect(state.awaitingFirstToken).toBe(false);

    vi.advanceTimersByTime(STALL_TIMEOUT_MS);
    expect(state.turnStalled).toBe(false);
    store.dispose();
  });

  it("a replayed running state mid-wait does not restart the clock", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("hello");
    const started = state.awaitingSince;
    expect(started).not.toBeNull();

    vi.advanceTimersByTime(5_000);
    store.applyEvent(sessionState("s1", "running"));
    expect(state.awaitingSince).toBe(started);

    // 25s from the SEND, not from the replayed state.
    vi.advanceTimersByTime(STALL_TIMEOUT_MS - 5_000);
    expect(state.turnStalled).toBe(true);
    store.dispose();
  });
});

// Queue and steer (TD-1704). The daemon already queues user messages — its
// SessionRunner drops a mid-turn `user_message` into an asyncio.Queue the loop
// drains at the next turn boundary — but that queue is write-only on the wire:
// nothing in the protocol edits or withdraws a message once handed over. So the
// rows live here, and these tests drive the store the way the composer does.
describe("queued messages", () => {
  /** A store bound to s1 with a turn provably in flight (a delta is the only
   *  honest evidence of one, per TD-1714). */
  function runningStore() {
    const bound = boundStore();
    bound.store.applyEvent(delta("s1", "thinking"));
    expect(showCancel(bound.state.turnState)).toBe(true);
    return bound;
  }

  function userSends(sent: ClientMessageUnion[]): string[] {
    return sent.filter((m) => m.type === "user_message").map((m) => m.content);
  }

  it("queues a message sent while a turn runs instead of handing it over", () => {
    const { store, state, sent } = runningStore();

    expect(store.sendUserMessage("check the tests too")).toBe(true);

    expect(state.queued.map((q) => q.text)).toEqual(["check the tests too"]);
    // Not on the wire: the daemon would take it and never give it back.
    expect(userSends(sent)).toEqual([]);
    // And not in the transcript either — it has not been said yet.
    expect(state.messages.some((m) => m.text === "check the tests too")).toBe(false);
    store.dispose();
  });

  it("sends straight through when no turn is live", () => {
    const { store, state, sent } = boundStore();

    expect(store.sendUserMessage("hello")).toBe(true);

    expect(state.queued).toEqual([]);
    expect(userSends(sent)).toEqual(["hello"]);
    store.dispose();
  });

  it("keeps queue order and gives each row its own id", () => {
    const { store, state } = runningStore();
    store.sendUserMessage("first");
    store.sendUserMessage("second");

    expect(state.queued.map((q) => q.text)).toEqual(["first", "second"]);
    expect(new Set(state.queued.map((q) => q.id)).size).toBe(2);
    // Queue ids share no namespace with conversation message ids.
    const messageIds = new Set(state.messages.map((m) => m.id));
    expect(state.queued.some((q) => messageIds.has(q.id))).toBe(false);
    store.dispose();
  });

  it("send-now hands one row over ahead of the rows before it", () => {
    const { store, state, sent } = runningStore();
    store.sendUserMessage("first");
    store.sendUserMessage("second");
    const second = state.queued[1].id;

    expect(store.sendQueuedNow(second)).toBe(true);

    expect(userSends(sent)).toEqual(["second"]);
    expect(state.queued.map((q) => q.text)).toEqual(["first"]);
    store.dispose();
  });

  it("send-now during a live turn does not restage the running turn's clock", () => {
    const { store, state } = runningStore();
    store.sendUserMessage("while you're at it");
    // A delta already landed, so no first-token wait is outstanding; the
    // shimmer must not come back over a turn that is visibly streaming.
    expect(state.awaitingFirstToken).toBe(false);

    store.sendQueuedNow(state.queued[0].id);

    expect(state.awaitingFirstToken).toBe(false);
    store.dispose();
  });

  it("editing a queued row replaces its text", () => {
    const { store, state } = runningStore();
    store.sendUserMessage("run the linter");
    const id = state.queued[0].id;

    store.editQueuedMessage(id, "run the linter and the type check");

    // Replaced in place: same row, same id, same position — not a second one.
    expect(state.queued).toHaveLength(1);
    expect(state.queued[0].id).toBe(id);
    expect(state.queued[0].text).toBe("run the linter and the type check");
    store.dispose();
  });

  it("sends the edited text, not the text as first typed", () => {
    const { store, state, sent } = runningStore();
    store.sendUserMessage("run the linter");
    store.editQueuedMessage(state.queued[0].id, "run the linter and the type check");

    store.applyEvent(turnComplete("s1"));

    expect(userSends(sent)).toEqual(["run the linter and the type check"]);
    store.dispose();
  });

  it("ignores an edit or a send-now for a row that is no longer queued", () => {
    const { store, state, sent } = runningStore();
    store.sendUserMessage("first");
    const id = state.queued[0].id;
    store.removeQueuedMessage(id);

    store.editQueuedMessage(id, "resurrected");
    expect(state.queued).toEqual([]);
    expect(store.sendQueuedNow(id)).toBe(false);
    expect(userSends(sent)).toEqual([]);
    store.dispose();
  });

  it("remove drops a row and never sends it", () => {
    const { store, state, sent } = runningStore();
    store.sendUserMessage("first");
    store.sendUserMessage("second");

    store.removeQueuedMessage(state.queued[0].id);

    expect(state.queued.map((q) => q.text)).toEqual(["second"]);
    expect(userSends(sent)).toEqual([]);
    store.dispose();
  });

  it("drains one row per turn end, in order, so the rest stay steerable", () => {
    const { store, state, sent } = runningStore();
    store.sendUserMessage("first");
    store.sendUserMessage("second");

    store.applyEvent(turnComplete("s1"));
    expect(userSends(sent)).toEqual(["first"]);
    // The tail is still here to edit or drop while the new turn runs.
    expect(state.queued.map((q) => q.text)).toEqual(["second"]);

    store.applyEvent(turnComplete("s1"));
    expect(userSends(sent)).toEqual(["first", "second"]);
    expect(state.queued).toEqual([]);
    store.dispose();
  });

  it("does not flush into a session that can no longer run a turn", () => {
    const { store, state, sent } = runningStore();
    store.sendUserMessage("first");

    // TD-1711: the daemon refuses a send to a terminal session, so flushing
    // here would void the text with nothing to show for it.
    store.applyEvent(sessionState("s1", "cancelled"));

    expect(userSends(sent)).toEqual([]);
    expect(state.queued.map((q) => q.text)).toEqual(["first"]);
    store.dispose();
  });

  it("drops queued text when the pane leaves the session it was typed against", () => {
    const { store, state, sent } = runningStore();
    store.sendUserMessage("meant for s1");

    store.selectSession("s2", "idle");

    expect(state.queued).toEqual([]);
    expect(userSends(sent)).toEqual([]);
    store.dispose();
  });

  it("clears the queue on dispose", () => {
    const { store, state } = runningStore();
    store.sendUserMessage("first");

    store.dispose();

    expect(state.queued).toEqual([]);
  });
});

describe("empty queue chrome", () => {
  const COMPONENT = readFileSync(
    resolve(process.cwd(), "src/lib/components/chat/QueuedMessages.svelte"),
    "utf-8",
  );

  /** The component's markup, between the script and the style blocks. */
  const template = COMPONENT.slice(
    COMPONENT.indexOf("</script>") + "</script>".length,
    COMPONENT.indexOf("<style>"),
  ).trim();

  it("is not empty, so a broken parse cannot pass this file", () => {
    expect(template.length).toBeGreaterThan(100);
  });

  it("renders nothing — not empty chrome — when nothing is queued", () => {
    expect(showQueue([])).toBe(false);

    // The stronger half of the criterion: there is no markup for a zero-length
    // queue to render *as*. Every element in the template sits inside the one
    // showQueue guard, which opens the template and closes it, so an empty
    // queue emits no container, no border and no reserved height above the
    // composer. An assertion that the container is merely empty would pass a
    // stray wrapper; this one cannot.
    expect(template.startsWith("{#if showQueue(queued)}")).toBe(true);
    expect(template.endsWith("{/if}")).toBe(true);
    expect(template.match(/\{#if /g)).toHaveLength(1);
  });

  it("shows the strip as soon as one message is queued", () => {
    expect(showQueue([{ id: "q1", text: "later", attachments: [] }])).toBe(true);
  });

  it("gives every queued row a send-now and a remove control", () => {
    expect(COMPONENT).toContain('aria-label="Send now"');
    expect(COMPONENT).toContain('aria-label="Remove from queue"');
  });
});

describe("archived sessions never win auto-bind (TD-1715)", () => {
  // Archiving is the user saying "not this one". Auto-bind adopting it on the
  // next refresh would undo that silently, and the pane would sit on a
  // conversation the rail no longer lists — a stranded composer by another
  // route. The daemon marks every row; the binding rule reads the mark.
  it("skips an archived session even when it is the newest live one", () => {
    const { deps, attached } = fakeDeps();
    const store = createChatStore(deps, createChatState());
    store.applyEvent(
      sessionList([
        { id: "older-live", updated: "2026-08-14T09:00:00Z", state: "idle" },
        { id: "newest-filed", updated: "2026-08-14T11:00:00Z", state: "idle", archived: true },
      ]),
    );
    expect(store.state.sessionId).toBe("older-live");
    expect(attached).toEqual(["older-live"]);
  });

  it("falls to the empty state when every live session is archived", () => {
    const { deps, attached } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    store.applyEvent(
      sessionList([
        { id: "filed-a", updated: "2026-08-14T09:00:00Z", state: "idle", archived: true },
        { id: "filed-b", updated: "2026-08-14T11:00:00Z", state: "running", archived: true },
      ]),
    );
    expect(state.sessionId).toBeNull();
    expect(state.turnState).toBeNull();
    expect(attached).toEqual([]);
  });

  it("moves the pane off the bound session when it is archived", () => {
    const { deps, attached, detached } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    store.applyEvent(
      sessionList([
        { id: "bound", updated: "2026-08-14T11:00:00Z", state: "idle" },
        { id: "other", updated: "2026-08-14T10:00:00Z", state: "idle" },
      ]),
    );
    expect(state.sessionId).toBe("bound");

    store.applyEvent(
      sessionList([
        { id: "bound", updated: "2026-08-14T11:30:00Z", state: "idle", archived: true },
        { id: "other", updated: "2026-08-14T10:00:00Z", state: "idle" },
      ]),
    );
    expect(state.sessionId).toBe("other");
    expect(detached).toContain("bound");
    expect(attached).toEqual(["bound", "other"]);
  });

  it("empties the pane when the bound session is archived and nothing else is live", () => {
    const { deps } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    store.applyEvent(sessionList([{ id: "only", updated: "2026-08-14T11:00:00Z", state: "idle" }]));
    expect(state.sessionId).toBe("only");

    store.applyEvent(
      sessionList([
        { id: "only", updated: "2026-08-14T11:30:00Z", state: "idle", archived: true },
      ]),
    );
    expect(state.sessionId).toBeNull();
    expect(state.messages).toEqual([]);
  });

  it("empties the pane when the bound session is deleted", () => {
    // Delete removes the row entirely, so this is the existing "not listed"
    // path — pinned here because it is the criterion, not an implementation
    // detail that may be refactored away.
    const { deps, detached } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    store.applyEvent(sessionList([{ id: "doomed", updated: "2026-08-14T11:00:00Z", state: "idle" }]));
    expect(state.sessionId).toBe("doomed");

    store.applyEvent(sessionList([]));
    expect(state.sessionId).toBeNull();
    expect(detached).toContain("doomed");
  });

  it("keeps the pane on a session that only moved project", () => {
    // A move keeps the session listed and unarchived, so the conversation
    // stays put — the composer is not stranded, it is re-homed.
    const { deps, detached } = fakeDeps();
    const state = createChatState();
    const store = createChatStore(deps, state);
    store.applyEvent(sessionList([{ id: "mover", updated: "2026-08-14T11:00:00Z", state: "idle" }]));
    store.applyEvent(sessionList([{ id: "mover", updated: "2026-08-14T11:30:00Z", state: "idle" }]));
    expect(state.sessionId).toBe("mover");
    expect(detached).toEqual([]);
  });
});

// ── Attachments (TD-1709) ─────────────────────────────────────────────
//
// The store's share of the story: attachments ride the same `user_message`
// they were composed with, they wait with a queued row rather than being
// dropped at the queue, and the sent row keeps chips rather than bytes.
// Whether a given file is *allowed* is decided in attachments.ts and, for
// real, in the daemon — none of that is re-litigated here.

function file(name: string, size = 4): { name: string; size: number; content_b64: string } {
  return { name, size, content_b64: "eA==" };
}

describe("attachments", () => {
  it("sends attachments on the user_message they were composed with", () => {
    const { store, sent } = boundStore();
    expect(store.sendUserMessage("look at this", [file("a.txt")])).toBe(true);
    const msg = sent.find((m) => m.type === "user_message");
    expect(msg).toEqual({
      type: "user_message",
      session_id: "s1",
      content: "look at this",
      attachments: [{ name: "a.txt", content_b64: "eA==" }],
    });
  });

  it("omits the field entirely when there are no attachments", () => {
    // Additive means an unchanged send is unchanged on the wire too, so the
    // message a client without this feature builds is byte-identical.
    const { store, sent } = boundStore();
    store.sendUserMessage("plain");
    const msg = sent.find((m) => m.type === "user_message");
    expect(msg).toEqual({ type: "user_message", session_id: "s1", content: "plain" });
  });

  it("keeps chips, not bytes, on the sent row", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("look", [file("a.txt", 12)]);
    const row = state.messages[state.messages.length - 1];
    expect(row.role).toBe("user");
    expect(row.attachments).toEqual([{ name: "a.txt", size: 12 }]);
  });

  it("leaves a plain message without an attachments field", () => {
    const { store, state } = boundStore();
    store.sendUserMessage("plain");
    expect(state.messages[state.messages.length - 1].attachments).toBeUndefined();
  });

  it("sends a message that is attachments and nothing else", () => {
    const { store, sent } = boundStore();
    expect(store.sendUserMessage("", [file("a.txt")])).toBe(true);
    expect(sent.some((m) => m.type === "user_message")).toBe(true);
  });

  it("still refuses a message that is neither text nor files", () => {
    const { store, sent } = boundStore();
    expect(store.sendUserMessage("   ", [])).toBe(false);
    expect(sent.some((m) => m.type === "user_message")).toBe(false);
  });

  it("attachments wait with a queued row instead of being dropped", () => {
    const { store, state, sent } = boundStore();
    store.applyEvent(delta("s1", "…"));  // a turn now owns the loop
    store.sendUserMessage("later", [file("q.txt")]);
    expect(sent.some((m) => m.type === "user_message")).toBe(false);
    expect(state.queued[0].attachments).toEqual([file("q.txt")]);

    store.applyEvent(turnComplete("s1"));
    const msg = sent.find((m) => m.type === "user_message");
    expect(msg).toMatchObject({ content: "later", attachments: [{ name: "q.txt", content_b64: "eA==" }] });
  });

  it("send-now carries the row's files too", () => {
    const { store, state, sent } = boundStore();
    store.applyEvent(delta("s1", "…"));
    store.sendUserMessage("steer", [file("s.txt")]);
    store.sendQueuedNow(state.queued[0].id);
    const msg = sent.find((m) => m.type === "user_message");
    expect(msg).toMatchObject({ attachments: [{ name: "s.txt", content_b64: "eA==" }] });
  });

  it("refuses to retry a message that carried files", () => {
    // The row keeps chips, not bytes: a silent resend without the files
    // would be a different message wearing the same label.
    const { store, sent } = boundStore();
    store.sendUserMessage("look", [file("a.txt")]);
    store.applyEvent(delta("s1", "hi"));
    store.applyEvent(turnComplete("s1"));
    const before = sent.filter((m) => m.type === "user_message").length;
    expect(store.retryLastUserMessage()).toBe(false);
    expect(sent.filter((m) => m.type === "user_message").length).toBe(before);
  });

  it("still retries a message that carried none", () => {
    const { store, sent } = boundStore();
    store.sendUserMessage("plain");
    store.applyEvent(delta("s1", "hi"));
    store.applyEvent(turnComplete("s1"));
    expect(store.retryLastUserMessage()).toBe(true);
    expect(sent.filter((m) => m.type === "user_message").length).toBe(2);
  });
});

// ── Reasoning deltas (TD-1901) ─────────────────────────────────────────
//
// A reasoning model streams thinking before it streams an answer. The store
// keeps the two apart on one message row, and treats thinking as proof the
// turn is alive — before this, a thinking model produced no store activity
// at all and the pane sat on the shimmer until the answer began.

function reasoning(sessionId: string, text: string): DaemonEventUnion {
  return { type: "assistant_reasoning", session_id: sessionId, delta: text, seq: 1 };
}

describe("reasoning deltas (TD-1901)", () => {
  it("opens a message with reasoning and empty text", () => {
    const { store, state } = boundStore();
    store.applyEvent(reasoning("s1", "Let me"));
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].reasoning).toBe("Let me");
    expect(state.messages[0].text).toBe("");
  });

  it("appends reasoning to the same row without touching text", () => {
    const { store, state } = boundStore();
    store.applyEvent(reasoning("s1", "Let me"));
    const message = state.messages[0];
    store.applyEvent(reasoning("s1", " think"));
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toBe(message); // identity stable, as for content
    expect(state.messages[0].reasoning).toBe("Let me think");
    expect(state.messages[0].text).toBe("");
  });

  it("content lands on the same row that carried the reasoning", () => {
    const { store, state } = boundStore();
    store.applyEvent(reasoning("s1", "Thinking"));
    store.applyEvent(delta("s1", "Answer"));
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].reasoning).toBe("Thinking");
    expect(state.messages[0].text).toBe("Answer");
  });

  it("reasoning ends the first-token wait", () => {
    // The defect this story fixes: reasoning was invisible, so the 25s
    // watchdog fired "No response yet" at a model that was answering fine.
    const { store, state } = boundStore();
    store.sendUserMessage("hi");
    expect(state.awaitingFirstToken).toBe(true);
    store.applyEvent(reasoning("s1", "Hmm"));
    expect(state.awaitingFirstToken).toBe(false);
    expect(state.turnStalled).toBe(false);
  });

  it("reasoning marks the turn running", () => {
    const { store, state } = boundStore();
    store.applyEvent(reasoning("s1", "Hmm"));
    expect(state.turnState).toBe("running");
  });

  it("stamps a duration once content begins, and only once", () => {
    const { store, state } = boundStore();
    store.applyEvent(reasoning("s1", "Thinking"));
    expect(state.messages[0].reasoningMs).toBeUndefined(); // still live
    store.applyEvent(delta("s1", "A"));
    const stamped = state.messages[0].reasoningMs;
    expect(stamped).toBeTypeOf("number");
    store.applyEvent(delta("s1", "B"));
    expect(state.messages[0].reasoningMs).toBe(stamped);
  });

  it("a turn that ends on reasoning alone stops counting", () => {
    // Cancel mid-thought, or a model that thought and then only called a
    // tool: without the seal the disclosure would count forever.
    const { store, state } = boundStore();
    store.applyEvent(reasoning("s1", "Thinking"));
    store.applyEvent(turnComplete("s1"));
    expect(state.messages[0].complete).toBe(true);
    expect(state.messages[0].reasoningMs).toBeTypeOf("number");
  });

  it("ignores reasoning for a session the pane is not bound to", () => {
    const { store, state } = boundStore();
    store.applyEvent(reasoning("s2", "elsewhere"));
    expect(state.messages).toHaveLength(0);
  });

  it("replays through the same reducer on attach", () => {
    const { store, state } = boundStore();
    const replay: DaemonEventUnion[] = [
      reasoning("s1", "Thought"),
      delta("s1", "Said"),
      turnComplete("s1"),
    ];
    for (const event of replay) store.applyEvent(event);
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].reasoning).toBe("Thought");
    expect(state.messages[0].text).toBe("Said");
    expect(state.messages[0].complete).toBe(true);
  });
});
