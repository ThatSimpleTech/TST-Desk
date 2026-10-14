// Unsent text survives a restart (TD-5019). Node environment, no DOM: the
// chat store is driven the way the pane drives it — the Composer writes
// `state.draft`, the pane's effect calls keepDraft — over an in-memory
// Storage, so "remount" is a second store reading what the first one left.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createChatState, createChatStore, type ChatDeps } from "./chat-store";
import { createDraftStore, draftKey, DRAFT_SAVE_DELAY_MS } from "./draft-store";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (key) => items.get(key) ?? null,
    key: (index) => [...items.keys()][index] ?? null,
    removeItem: (key) => {
      items.delete(key);
    },
    setItem: (key, value) => {
      items.set(key, String(value));
    },
  };
}

function throwingStorage(): Storage {
  const fail = (): never => {
    throw new Error("SecurityError: storage is disabled");
  };
  return { length: 0, clear: fail, getItem: fail, key: fail, removeItem: fail, setItem: fail };
}

function sessionList(...ids: string[]): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 1,
    sessions: ids.map((id, i) => ({
      session_id: id,
      workspace_path: "/workspace",
      state: "idle",
      created_at: `2026-10-14T10:0${i}:00Z`,
      updated_at: `2026-10-14T10:0${i}:00Z`,
      event_count: 0,
      archived: false,
      starred: false,
    })),
  };
}

function delta(sessionId: string, text: string): DaemonEventUnion {
  return { type: "assistant_delta", session_id: sessionId, delta: text, seq: 2 };
}

function userTurn(sessionId: string, content: string): DaemonEventUnion {
  return { type: "user_turn", session_id: sessionId, turn_id: "t1", content, seq: 3 };
}

function turnComplete(sessionId: string): DaemonEventUnion {
  return { type: "turn_complete", session_id: sessionId, tokens: 1, cost: 0, tier: "worker", duration: 1, failed: false, error_code: null, seq: 4 };
}

/** A chat store bound to the newest listed session ("s1" by default), the
 *  way a pane mount ends up: list, bind, restore. */
function mounted(storage: () => Storage | null, ...ids: string[]) {
  const sent: ClientMessageUnion[] = [];
  const deps: ChatDeps = {
    send: (msg) => {
      sent.push(msg);
      return true;
    },
    attach: () => {},
    detach: () => {},
    drafts: createDraftStore(storage),
  };
  const state = createChatState();
  const store = createChatStore(deps, state);
  store.applyEvent(sessionList(...(ids.length === 0 ? ["s1"] : ids)));
  const userSends = () => sent.filter((m) => m.type === "user_message");
  return { store, state, sent, userSends };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("draft store", () => {
  it("saves the draft after 300 ms of quiet and a remount restores it", () => {
    const storage = memoryStorage();
    const first = mounted(() => storage);
    first.state.draft = "half a thought";
    first.store.keepDraft();

    vi.advanceTimersByTime(DRAFT_SAVE_DELAY_MS - 1);
    expect(storage.getItem(draftKey("s1"))).toBeNull();
    vi.advanceTimersByTime(1);
    expect(storage.getItem(draftKey("s1"))).not.toBeNull();
    first.store.dispose();

    const second = mounted(() => storage);
    expect(second.state.draft).toBe("half a thought");
  });

  it("keys the draft tst.draft.v1.<sessionId>, bridged ids included", () => {
    expect(draftKey("s1")).toBe("tst.draft.v1.s1");
    expect(draftKey("abc123:mch_0123456789ab")).toBe("tst.draft.v1.abc123:mch_0123456789ab");
  });

  it("keeps the latest text when the pane goes away inside the debounce", () => {
    const storage = memoryStorage();
    const first = mounted(() => storage);
    first.state.draft = "typed just before the window closed";
    first.store.keepDraft();
    first.store.dispose();

    expect(mounted(() => storage).state.draft).toBe("typed just before the window closed");
  });

  it("clears the saved draft once the daemon takes the send", () => {
    const storage = memoryStorage();
    const { store, state } = mounted(() => storage);
    state.draft = "ship it";
    store.keepDraft();
    vi.advanceTimersByTime(DRAFT_SAVE_DELAY_MS);

    store.sendUserMessage("ship it");
    // The Composer empties itself on submit; the pane's effect follows.
    state.draft = "";
    store.keepDraft();
    store.applyEvent(userTurn("s1", "ship it"));

    expect(storage.getItem(draftKey("s1"))).toBeNull();
    store.dispose();
    expect(mounted(() => storage).state.draft).toBe("");
  });

  it("a storage that throws never throws out, and the draft still works in memory", () => {
    for (const storage of [() => throwingStorage(), (): Storage => { throw new Error("SecurityError"); }]) {
      const { store, state, userSends } = mounted(storage);
      expect(state.sessionId).toBe("s1");
      expect(state.draft).toBe("");
      state.draft = "kept in memory";
      expect(() => {
        store.keepDraft();
        vi.advanceTimersByTime(DRAFT_SAVE_DELAY_MS);
      }).not.toThrow();
      expect(state.draft).toBe("kept in memory");
      expect(store.sendUserMessage("kept in memory")).toBe(true);
      expect(() => store.applyEvent(userTurn("s1", "kept in memory"))).not.toThrow();
      expect(userSends()).toHaveLength(1);
      expect(() => store.dispose()).not.toThrow();
    }
  });

  it("an unreadable saved entry restores nothing", () => {
    const storage = memoryStorage();
    storage.setItem(draftKey("s1"), "{not json");
    expect(mounted(() => storage).state.draft).toBe("");
    storage.setItem(draftKey("s1"), JSON.stringify({ text: 7, queued: "no" }));
    expect(mounted(() => storage).state.draft).toBe("");
  });

  it("queued rows come back as draft text, never as a send, and the queue stays empty", () => {
    const storage = memoryStorage();
    const first = mounted(() => storage);
    first.store.applyEvent(delta("s1", "working"));
    first.store.sendUserMessage("first queued");
    first.store.sendUserMessage("second queued");
    first.store.sendUserMessage("with a file", [{ name: "notes.txt", size: 4, content_b64: "c2VjcmV0" }]);
    expect(first.state.queued).toHaveLength(3);
    first.state.draft = "still typing";
    first.store.keepDraft();
    vi.advanceTimersByTime(DRAFT_SAVE_DELAY_MS);

    // Text-only rows, and never an attachment's bytes.
    const saved = storage.getItem(draftKey("s1")) ?? "";
    expect(saved).not.toContain("c2VjcmV0");
    expect(saved).not.toContain("with a file");
    first.store.dispose();

    const second = mounted(() => storage);
    expect(second.state.draft).toBe("first queued\n\nsecond queued\n\nstill typing");
    expect(second.state.queued).toEqual([]);
    second.store.applyEvent(turnComplete("s1"));
    expect(second.userSends()).toEqual([]);
  });

  it("keeps each session's draft to itself across a switch and back", () => {
    const storage = memoryStorage();
    const { store, state } = mounted(() => storage, "s1", "s2");
    expect(state.sessionId).toBe("s2");
    state.draft = "for s2";
    store.keepDraft();

    store.selectSession("s1", null);
    expect(state.draft).toBe("");
    state.draft = "for s1";
    store.keepDraft();
    vi.advanceTimersByTime(DRAFT_SAVE_DELAY_MS);

    store.selectSession("s2", null);
    expect(state.draft).toBe("for s2");
    store.selectSession("s1", null);
    expect(state.draft).toBe("for s1");
    expect(JSON.parse(storage.getItem(draftKey("s2")) ?? "null")).toMatchObject({ text: "for s2" });
  });

  it("an emptied draft removes its key instead of storing blanks", () => {
    const storage = memoryStorage();
    const { store, state } = mounted(() => storage);
    state.draft = "draft";
    store.keepDraft();
    vi.advanceTimersByTime(DRAFT_SAVE_DELAY_MS);
    state.draft = "   ";
    store.keepDraft();
    vi.advanceTimersByTime(DRAFT_SAVE_DELAY_MS);
    expect(storage.length).toBe(0);
  });
});
