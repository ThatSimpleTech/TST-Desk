// Held messages after a restart (TD-5155). Node environment: the store is
// driven exactly as the connection fan-out and the components drive it.

import { describe, expect, it } from "vitest";
import { droppedNote } from "./chat-held";
import { createChatState, createChatStore, type ChatDeps } from "./chat-store";
import type { ClientMessageUnion, DaemonEventUnion, QueuedMessageEntry } from "./protocol";

function boundStore(sendResult = true) {
  const sent: ClientMessageUnion[] = [];
  const deps: ChatDeps = {
    send: (msg) => {
      sent.push(msg);
      return sendResult;
    },
    attach: () => {},
    detach: () => {},
    drafts: { schedule: () => {}, flush: () => {}, restore: () => "" },
  };
  const state = createChatState();
  const store = createChatStore(deps, state);
  store.applyEvent({
    type: "session_list",
    seq: 1,
    sessions: ["s1", "s2"].map((id, i) => ({
      session_id: id,
      workspace_path: "/workspace",
      state: "idle",
      created_at: "2026-10-19T10:00:00Z",
      updated_at: `2026-10-19T1${i === 0 ? 1 : 0}:00:00Z`,
      event_count: 0,
      archived: false,
      starred: false,
    })),
  });
  return { store, state, sent };
}

function held(messages: Partial<QueuedMessageEntry>[], sessionId = "s1"): DaemonEventUnion {
  return {
    type: "queued_messages",
    session_id: sessionId,
    seq: 9,
    messages: messages.map((m, i) => ({
      queued_id: m.queued_id ?? `q${i + 1}`,
      content: m.content ?? `message ${i + 1}`,
      images_dropped: m.images_dropped ?? 0,
    })),
  };
}

describe("held messages", () => {
  it("shows the daemon's latest list, whole", () => {
    const { store, state } = boundStore();
    store.applyEvent(held([{ content: "also check b" }, { content: "and c", images_dropped: 2 }]));
    expect(state.held.map((r) => [r.id, r.text, r.imagesDropped])).toEqual([
      ["q1", "also check b", 0],
      ["q2", "and c", 2],
    ]);
    store.applyEvent(held([{ queued_id: "q2", content: "and c" }]));
    expect(state.held.map((r) => r.id)).toEqual(["q2"]);
  });

  it("ignores another session's list, and a bind starts empty", () => {
    const { store, state } = boundStore();
    store.applyEvent(held([{ content: "not mine" }], "s2"));
    expect(state.held).toEqual([]);
    store.applyEvent(held([{ content: "mine" }]));
    store.selectSession("s2", "idle");
    expect(state.held).toEqual([]);
  });

  it("Send asks the daemon and keeps the row until its next list", () => {
    const { store, state, sent } = boundStore();
    store.applyEvent(held([{ content: "also check b" }]));
    expect(store.sendHeldMessage("q1")).toBe(true);
    expect(sent).toEqual([{ type: "send_queued", session_id: "s1", queued_id: "q1" }]);
    // No user_message, no local echo: the user_turn will bring the row.
    expect(state.messages).toEqual([]);
    expect(state.held[0].pending).toBe(true);
    expect(state.awaitingFirstToken).toBe(true);
    // A second click cannot ask twice.
    expect(store.sendHeldMessage("q1")).toBe(false);
    expect(store.removeHeldMessage("q1")).toBe(false);
    expect(sent).toHaveLength(1);
    store.applyEvent(held([]));
    expect(state.held).toEqual([]);
  });

  it("Remove asks the daemon to drop it", () => {
    const { store, sent } = boundStore();
    store.applyEvent(held([{ content: "never mind" }]));
    expect(store.removeHeldMessage("q1")).toBe(true);
    expect(sent).toEqual([{ type: "remove_queued", session_id: "s1", queued_id: "q1" }]);
  });

  it("a refused Send leaves the row held and the composer alone", () => {
    const { store, state } = boundStore();
    state.draft = "typing";
    store.applyEvent(held([{ content: "also check b" }]));
    store.sendHeldMessage("q1");
    store.applyEvent({
      type: "error",
      seq: 1,
      code: "daemon_draining",
      message: "TST Desk is about to restart.",
      session_id: "s1",
      in_reply_to: "send_queued",
    });
    expect(state.held.map((r) => [r.id, r.pending])).toEqual([["q1", false]]);
    expect(state.draft).toBe("typing");
    expect(state.awaitingFirstToken).toBe(false);
  });

  it("a send the daemon held loses its echo row: it is held, not in the transcript", () => {
    const { store, state } = boundStore();
    store.applyEvent({ type: "session_state", session_id: "s1", state: "paused", seq: 2 });
    expect(store.sendUserMessage("also check b")).toBe(true);
    expect(state.messages.map((m) => m.text)).toEqual(["also check b"]);
    // The daemon restarted; the window re-attached and got the list.
    store.applyEvent(held([{ content: "also check b" }]));
    expect(state.messages).toEqual([]);
    expect(state.held.map((r) => r.text)).toEqual(["also check b"]);
  });

  it("does nothing for an id that is not held", () => {
    const { store, sent } = boundStore();
    expect(store.sendHeldMessage("q9")).toBe(false);
    expect(sent).toEqual([]);
  });
});

describe("droppedNote", () => {
  it("names lost images, and is null when none were", () => {
    expect(droppedNote(0)).toBeNull();
    expect(droppedNote(1)).toBe("1 attached image was not kept and will not be sent");
    expect(droppedNote(3)).toBe("3 attached images were not kept and will not be sent");
  });
});
