// A turn a restart cut off (TD-5018), Continue (TD-5019) and a send the
// daemon refused while it drains for a restart (TD-5023). Node environment:
// the store is driven with the events a reconnect replays after the daemon
// sealed the cut turn on its next start.

import { describe, expect, it } from "vitest";
import { createChatState, createChatStore, showCancel, type ChatDeps } from "./chat-store";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

function sessionList(): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 1,
    sessions: [
      {
        session_id: "s1",
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-10-14T10:00:00Z",
        updated_at: "2026-10-14T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  };
}

function turnComplete(errorCode: string | null, sessionId = "s1"): DaemonEventUnion {
  return {
    type: "turn_complete",
    session_id: sessionId,
    tokens: 0,
    cost: 0,
    tier: "brain",
    duration: 0,
    failed: errorCode !== null,
    error_code: errorCode,
    seq: 9,
  };
}

function refusal(code: string, sessionId = "s1"): DaemonEventUnion {
  return {
    type: "error",
    code,
    message: "TST Desk is about to restart. Your message was not sent; send it again in a few seconds.",
    session_id: sessionId,
    seq: 10,
  } as DaemonEventUnion;
}

function bound() {
  const sent: ClientMessageUnion[] = [];
  const deps: ChatDeps = {
    send: (msg) => {
      sent.push(msg);
      return true;
    },
    attach: () => {},
    detach: () => {},
  };
  const state = createChatState();
  const store = createChatStore(deps, state);
  store.applyEvent(sessionList());
  const userSends = () => sent.filter((m) => m.type === "user_message").map((m) => m.content);
  return { store, state, userSends };
}

/** The replay a reconnect gets after the daemon sealed a cut turn. */
function replayCutTurn(store: ReturnType<typeof bound>["store"]): void {
  store.applyEvent({ type: "user_turn", session_id: "s1", turn_id: "t1", content: "refactor the parser", seq: 2 });
  store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Reading parser.py", seq: 3 });
  store.applyEvent({ type: "session_state", session_id: "s1", state: "running", seq: 4 });
}

describe("a turn cut off by a restart", () => {
  it("is sealed by the replayed turn_complete and offers Continue without flushing the queue", () => {
    const { store, state, userSends } = bound();
    replayCutTurn(store);
    // Before the seal arrives it looks live, so Enter parks text in the queue.
    expect(showCancel(state.turnState)).toBe(true);
    store.sendUserMessage("then run the tests");
    expect(state.queued.map((q) => q.text)).toEqual(["then run the tests"]);

    store.applyEvent(turnComplete("interrupted_by_restart"));

    const last = state.messages[state.messages.length - 1];
    expect(last.role).toBe("assistant");
    expect(last.complete).toBe(true);
    expect(state.turnState).toBeNull();
    expect(showCancel(state.turnState) || state.awaitingFirstToken).toBe(false);
    expect(state.lastTurnError).toBe("interrupted_by_restart");
    // The queued row was written for a reply that never came: it waits.
    expect(state.queued.map((q) => q.text)).toEqual(["then run the tests"]);
    expect(userSends()).toEqual([]);
  });

  it("stays sealed when the revived session's running is replayed after the seal", () => {
    const { store, state } = bound();
    store.applyEvent({ type: "user_turn", session_id: "s1", turn_id: "t1", content: "go", seq: 2 });
    store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Working", seq: 3 });
    store.applyEvent(turnComplete("interrupted_by_restart"));
    store.applyEvent({ type: "session_state", session_id: "s1", state: "running", seq: 11 });
    expect(state.turnState).toBeNull();
    expect(state.lastTurnError).toBe("interrupted_by_restart");
  });

  it("Continue sends exactly one ordinary user_message, and the next turn_complete clears it", () => {
    const { store, state, userSends } = bound();
    replayCutTurn(store);
    store.applyEvent(turnComplete("interrupted_by_restart"));
    state.draft = "an unrelated draft";

    expect(store.continueTurn()).toBe(true);

    expect(userSends()).toEqual(["Continue"]);
    // Continue is its own message: the user's draft is not sent or dropped.
    expect(state.draft).toBe("an unrelated draft");
    expect(state.awaitingFirstToken).toBe(true);

    store.applyEvent({ type: "user_turn", session_id: "s1", turn_id: "t2", content: "Continue", seq: 12 });
    store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Picking up", seq: 13 });
    store.applyEvent(turnComplete(null));
    expect(state.lastTurnError).toBeNull();
    expect(userSends()).toEqual(["Continue"]);
  });

  it("Continue works after a turn that carried attachments", () => {
    const { store, userSends } = bound();
    store.sendUserMessage("look at this", [{ name: "a.txt", size: 1, content_b64: "eA==" }]);
    store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Looking", seq: 3 });
    store.applyEvent(turnComplete("interrupted_by_restart"));
    // Retry refuses this turn (it cannot resend the files); Continue does not.
    expect(store.retryLastUserMessage()).toBe(false);
    expect(store.continueTurn()).toBe(true);
    expect(userSends()).toEqual(["look at this", "Continue"]);
  });

  it("other turn endings still flush the queue head, and the flushed send clears the error", () => {
    const quiet = bound();
    quiet.store.applyEvent(turnComplete("rate_limited"));
    expect(quiet.state.lastTurnError).toBe("rate_limited");

    const { store, state, userSends } = bound();
    store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Working", seq: 3 });
    store.sendUserMessage("next");
    store.applyEvent(turnComplete("rate_limited"));
    expect(userSends()).toEqual(["next"]);
    expect(state.lastTurnError).toBeNull();
  });

  it("a session switch forgets the last turn's error", () => {
    const { store, state } = bound();
    store.applyEvent(turnComplete("interrupted_by_restart"));
    store.selectSession("s2", null);
    expect(state.lastTurnError).toBeNull();
  });
});

describe("a send refused while the daemon drains for a restart", () => {
  it("ends the wait, takes the echo row back out and puts the text back in the draft", () => {
    const { store, state, userSends } = bound();
    store.sendUserMessage("deploy notes");
    // The Composer empties itself on submit.
    state.draft = "";
    expect(state.awaitingFirstToken).toBe(true);
    expect(state.messages.map((m) => m.text)).toEqual(["deploy notes"]);

    store.applyEvent(refusal("daemon_draining"));

    expect(state.awaitingFirstToken).toBe(false);
    expect(state.turnStalled).toBe(false);
    expect(state.draft).toBe("deploy notes");
    expect(state.messages).toEqual([]);
    expect(state.queued).toEqual([]);
    expect(userSends()).toEqual(["deploy notes"]);
  });

  it("puts a refused text ahead of what was typed since", () => {
    const { store, state } = bound();
    store.sendUserMessage("first");
    state.draft = "typed after";
    store.applyEvent(refusal("daemon_draining"));
    expect(state.draft).toBe("first\n\ntyped after");
  });

  it("gives back every refused text, in the order it was sent", () => {
    const { store, state } = bound();
    // Accepted earlier and still waiting in the daemon's own queue.
    store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Working", seq: 3 });
    store.sendUserMessage("steer");
    store.sendQueuedNow(state.queued[0].id);
    // Two sends the hold refuses.
    store.sendUserMessage("one");
    store.sendQueuedNow(state.queued[0].id);
    store.sendUserMessage("two");
    store.sendQueuedNow(state.queued[0].id);

    store.applyEvent(refusal("daemon_draining"));
    store.applyEvent(refusal("daemon_draining"));

    expect(state.draft).toBe("one\n\ntwo");
  });

  it("does not seal a reply that is still streaming", () => {
    const { store, state } = bound();
    store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Still going", seq: 3 });
    store.sendUserMessage("steer now");
    store.sendQueuedNow(state.queued[0].id);
    store.applyEvent(refusal("daemon_draining"));
    const reply = state.messages.find((m) => m.role === "assistant");
    expect(reply?.complete).toBe(false);
    expect(showCancel(state.turnState)).toBe(true);
    expect(state.draft).toBe("steer now");
  });

  it("ignores a refusal addressed at another session", () => {
    const { store, state } = bound();
    store.sendUserMessage("hello");
    store.applyEvent(refusal("daemon_draining", "elsewhere"));
    expect(state.awaitingFirstToken).toBe(true);
    expect(state.draft).toBe("");
  });

  it("a send the daemon took is not given back by a later refusal", () => {
    const { store, state } = bound();
    store.sendUserMessage("taken");
    store.applyEvent({ type: "user_turn", session_id: "s1", turn_id: "t1", content: "taken", seq: 2 });
    store.applyEvent(refusal("daemon_draining"));
    expect(state.draft).toBe("");
    expect(state.messages.map((m) => m.text)).toEqual(["taken"]);
  });
});
