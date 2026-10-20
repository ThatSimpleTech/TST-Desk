// Steer a running turn (TD-5112). Send now while a turn runs hands the
// message to the daemon, which gives it to the running turn at the next
// tool-batch boundary. Until its user_turn comes back the row waits in the
// queue strip, read-only, and says so; then it lands in the transcript at
// the point the model read it. Stop still stops the turn.

import { describe, expect, it } from "vitest";
import { waitingLabel } from "./chat-queue";
import { createChatState, createChatStore, showCancel, type ChatDeps } from "./chat-store";
import { createDraftStore } from "./draft-store";
import type { ClientMessageUnion, DaemonEventUnion, UserTurn } from "./protocol";

function sessionList(): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 1,
    sessions: [
      {
        session_id: "s1",
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-10-20T10:00:00Z",
        updated_at: "2026-10-20T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  };
}

function userTurn(turnId: string, content: string, steer: UserTurn["steer"] = null): DaemonEventUnion {
  return { type: "user_turn", session_id: "s1", turn_id: turnId, content, steer, seq: 20 };
}

function steerPoint(turnId = "t1") {
  return { turn_id: turnId, step: 1, after_tool_call_ids: ["tc-1"] };
}

function turnComplete(): DaemonEventUnion {
  return {
    type: "turn_complete",
    session_id: "s1",
    tokens: 1,
    cost: 0,
    tier: "brain",
    duration: 1,
    failed: false,
    error_code: null,
    seq: 30,
  };
}

/** A store bound to s1 with a turn running: its user_turn, a delta and a tool call. */
function running() {
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
  store.sendUserMessage("build it");
  store.applyEvent(userTurn("t1", "build it"));
  store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Building.", seq: 21 });
  store.applyEvent({
    type: "tool_call",
    session_id: "s1",
    tool_call_id: "tc-1",
    name: "shell",
    arguments: { command: "make" },
    decision_class: "A",
    seq: 22,
  } as DaemonEventUnion);
  expect(showCancel(state.turnState)).toBe(true);
  const userSends = () =>
    sent.filter((m) => m.type === "user_message").map((m) => (m as { content: string }).content);
  return { store, state, sent, userSends };
}

describe("Send now while a turn runs", () => {
  it("sends the message and keeps the row, waiting and read-only, out of the transcript", () => {
    const { store, state, sent, userSends } = running();
    store.sendUserMessage("  also run the linter  ");
    const id = state.queued[0].id;

    expect(store.sendQueuedNow(id)).toBe(true);

    expect(userSends()).toEqual(["build it", "also run the linter"]);
    // It asks the daemon to steer; the turn's own send did not.
    const wire = sent.filter((m) => m.type === "user_message");
    expect(wire[0]).toEqual({ type: "user_message", session_id: "s1", content: "build it" });
    expect(wire[1]).toEqual({
      type: "user_message",
      session_id: "s1",
      content: "also run the linter",
      steer: true,
    });
    expect(state.queued).toMatchObject([{ id, text: "also run the linter", waiting: true }]);
    // Not in the transcript yet: the model has not read it.
    expect(state.messages.filter((m) => m.role === "user").map((m) => m.text)).toEqual(["build it"]);
    // The daemon has it now, so it cannot be edited, removed or sent twice.
    store.editQueuedMessage(id, "something else");
    store.removeQueuedMessage(id);
    expect(store.sendQueuedNow(id)).toBe(false);
    expect(state.queued).toMatchObject([{ id, text: "also run the linter", waiting: true }]);
    expect(userSends()).toHaveLength(2);
  });

  it("Stop still stops the turn while a message waits", () => {
    const { store, state, sent } = running();
    store.sendUserMessage("also lint");
    store.sendQueuedNow(state.queued[0].id);

    expect(showCancel(state.turnState)).toBe(true);
    expect(store.cancelTurn()).toBe(true);
    expect(sent[sent.length - 1]).toEqual({ type: "stop_turn", session_id: "s1" });
  });

  it("says what it waits for, by engine", () => {
    expect(waitingLabel("native")).toBe("Sent, waiting for the next step");
    expect(waitingLabel(null)).toBe("Sent, waiting for the next step");
    // Grok runs its own tools, so there the message waits for the turn's end.
    expect(waitingLabel("grok")).toBe("Sent, waiting for this turn to end");
  });

  it("is never saved as draft text: it was sent", () => {
    const stored = new Map<string, string>();
    const storage = {
      getItem: (k: string) => stored.get(k) ?? null,
      setItem: (k: string, v: string) => void stored.set(k, v),
      removeItem: (k: string) => void stored.delete(k),
    } as unknown as Storage;
    const drafts = createDraftStore(() => storage);
    drafts.schedule("s1", "", [
      { id: "q1", text: "sent already", attachments: [], waiting: true },
      { id: "q2", text: "still mine", attachments: [] },
    ]);
    drafts.flush("s1");
    expect(createDraftStore(() => storage).restore("s1")).toBe("still mine");
  });
});

describe("the daemon hands it to the turn", () => {
  it("lands after what the model already said, and the reply after it is a new row", () => {
    const { store, state } = running();
    store.sendUserMessage("also lint");
    store.sendQueuedNow(state.queued[0].id);

    store.applyEvent({
      type: "tool_result",
      session_id: "s1",
      tool_call_id: "tc-1",
      status: "success",
      output: "ok",
      seq: 23,
    } as DaemonEventUnion);
    store.applyEvent(userTurn("t2", "also lint", steerPoint()));

    expect(state.queued).toEqual([]);
    const rows = state.messages.map((m) => [m.role, m.text, m.complete]);
    expect(rows).toEqual([
      ["user", "build it", true],
      ["assistant", "Building.", true],
      ["user", "also lint", true],
    ]);
    expect(state.messages[2].turnId).toBe("t2");
    // Still one turn, still running: Stop stays.
    expect(showCancel(state.turnState)).toBe(true);

    store.applyEvent({ type: "assistant_delta", session_id: "s1", delta: "Linting too.", seq: 24 });
    expect(state.messages.map((m) => [m.role, m.text])).toEqual([
      ["user", "build it"],
      ["assistant", "Building."],
      ["user", "also lint"],
      ["assistant", "Linting too."],
    ]);
  });

  it("keeps the chips it was sent with", () => {
    const { store, state } = running();
    store.sendUserMessage("look at this", [{ name: "notes.txt", size: 5, content_b64: "aGVsbG8=" }]);
    store.sendQueuedNow(state.queued[0].id);
    store.applyEvent(userTurn("t2", "look at this\n\n[Attached: notes.txt]", steerPoint()));
    const row = state.messages[state.messages.length - 1];
    expect(row.text).toBe("look at this");
    expect(row.attachments).toEqual([{ name: "notes.txt", size: 5 }]);
  });

  it("a user_turn from another device steers the transcript the same way", () => {
    const { store, state } = running();
    store.applyEvent(userTurn("t2", "from the phone", steerPoint()));
    expect(state.messages.map((m) => [m.role, m.text, m.complete])).toEqual([
      ["user", "build it", true],
      ["assistant", "Building.", true],
      ["user", "from the phone", true],
    ]);
  });
});

describe("a waiting message no tool batch took", () => {
  it("starts the next turn before the queue's next row goes", () => {
    const { store, state, userSends } = running();
    store.sendUserMessage("steer");
    store.sendQueuedNow(state.queued[0].id);
    store.sendUserMessage("after that");

    // The turn ended without another batch: the daemon starts "steer" next.
    // Its clock starts as a send's does, but only the daemon says a turn runs.
    store.applyEvent(turnComplete());
    expect(userSends()).toEqual(["build it", "steer"]);
    expect(state.turnState).toBeNull();
    expect(state.awaitingFirstToken).toBe(true);

    store.applyEvent(userTurn("t2", "steer"));
    expect(state.queued.map((q) => q.text)).toEqual(["after that"]);
    store.applyEvent(turnComplete());
    expect(userSends()).toEqual(["build it", "steer", "after that"]);
  });

  it("leaves the strip when a restart holds it instead", () => {
    const { store, state } = running();
    store.sendUserMessage("steer");
    store.sendQueuedNow(state.queued[0].id);
    store.applyEvent({
      type: "queued_messages",
      session_id: "s1",
      messages: [{ queued_id: "q-1", content: "steer", images_dropped: 0 }],
      seq: 40,
    } as DaemonEventUnion);
    expect(state.queued).toEqual([]);
    expect(state.held.map((h) => h.text)).toEqual(["steer"]);
  });

  it("is an ordinary row again when the session ends before any turn took it", () => {
    const { store, state } = running();
    store.sendUserMessage("steer");
    store.sendQueuedNow(state.queued[0].id);
    store.applyEvent({ type: "session_state", session_id: "s1", state: "cancelled", seq: 42 });
    expect(state.queued.map((q) => [q.text, q.waiting])).toEqual([["steer", false]]);
    store.removeQueuedMessage(state.queued[0].id);
    expect(state.queued).toEqual([]);
  });

  it("goes back to the composer when the daemon refuses it", () => {
    const { store, state } = running();
    store.sendUserMessage("steer");
    store.sendQueuedNow(state.queued[0].id);
    store.applyEvent({
      type: "error",
      code: "session_not_running",
      message: "This session can no longer run turns.",
      session_id: "s1",
      seq: 41,
    } as DaemonEventUnion);
    expect(state.queued).toEqual([]);
    expect(state.draft).toBe("steer");
  });
});

describe("a waiting message the daemon refuses", () => {
  /** A Send now with an image, then a row queued behind it. */
  function refusedSetup() {
    const env = running();
    const { store, state } = env;
    store.sendUserMessage("see pic", [{ name: "pic.png", size: 3, content_b64: "AAAA" }]);
    store.sendQueuedNow(state.queued[0].id);
    store.sendUserMessage("next");
    return env;
  }

  function noVision(): DaemonEventUnion {
    return {
      type: "error",
      code: "attachment_no_vision",
      message: "The active model can't read images.",
      session_id: "s1",
      in_reply_to: "user_message",
      seq: 0,
    } as DaemonEventUnion;
  }

  it("leaves the strip and goes back to the composer, whatever the code", () => {
    const { store, state, userSends } = refusedSetup();
    store.applyEvent(noVision());
    expect(state.queued.map((q) => [q.text, q.waiting ?? null])).toEqual([["next", null]]);
    expect(state.draft).toBe("see pic");
    // The turn still runs: its Stop stays, and the next row waits for its end.
    expect(showCancel(state.turnState)).toBe(true);

    store.applyEvent(turnComplete());
    expect(userSends()).toEqual(["build it", "see pic", "next"]);
    expect(state.queued).toEqual([]);
  });

  it("does not leave the window holding a turn when the refusal crosses turn_complete", () => {
    const { store, state, userSends } = refusedSetup();
    store.applyEvent(turnComplete());
    store.applyEvent(noVision());

    expect(state.turnState).toBeNull();
    expect(state.awaitingFirstToken).toBe(false);
    expect(state.draft).toBe("see pic");
    // The row behind it is an ordinary row again, and a new message goes out.
    expect(state.queued.map((q) => q.text)).toEqual(["next"]);
    expect(store.sendUserMessage("typed later")).toBe(true);
    expect(userSends()).toEqual(["build it", "see pic", "typed later"]);
  });

  it("names a refused send by its code when an older computer leaves in_reply_to out", () => {
    const { store, state } = refusedSetup();
    store.applyEvent({ ...noVision(), in_reply_to: undefined } as DaemonEventUnion);
    expect(state.queued.map((q) => q.text)).toEqual(["next"]);
    expect(state.draft).toBe("see pic");
  });

  it("leaves a waiting row alone for an error that refuses no send", () => {
    const { store, state } = refusedSetup();
    store.applyEvent({
      type: "error",
      code: "queued_not_found",
      message: "That message is no longer held.",
      session_id: "s1",
      in_reply_to: "send_queued",
      seq: 0,
    } as DaemonEventUnion);
    expect(state.queued.map((q) => [q.text, q.waiting ?? null])).toEqual([
      ["see pic", true],
      ["next", null],
    ]);
    expect(state.draft).toBe("");
  });
});

describe("files with no text, sent now while a turn runs", () => {
  it("are echoed and wait for a turn of their own, so nothing is left waiting", () => {
    const { store, state, sent } = running();
    store.sendUserMessage("", [{ name: "notes.txt", size: 5, content_b64: "aGVsbG8=" }]);
    expect(store.sendQueuedNow(state.queued[0].id)).toBe(true);

    expect(state.queued).toEqual([]);
    const wire = sent.filter((m) => m.type === "user_message");
    expect(wire[wire.length - 1]).not.toHaveProperty("steer");
    const last = state.messages[state.messages.length - 1];
    expect([last.role, last.attachments]).toEqual(["user", [{ name: "notes.txt", size: 5 }]]);
    // The running turn's clock is not restaged.
    expect(state.awaitingFirstToken).toBe(false);
  });
});
