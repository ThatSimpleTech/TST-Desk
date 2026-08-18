import { describe, it, expect } from "vitest";
import { createChatState, type ChatMessage } from "./chat-store";
import { createBranchSnaps } from "./chat-fork";

function user(text: string, userIndex: number): ChatMessage {
  return {
    id: `u${userIndex}`,
    role: "user",
    text,
    complete: true,
    at: 0,
    userIndex,
    siblingIndex: 0,
    siblingCount: 1,
  };
}

function assistant(text: string): ChatMessage {
  return { id: `a-${text}`, role: "assistant", text, complete: true, at: 0 };
}

describe("applyReset with no snapshot", () => {
  it("replaces the user row and drops everything after it", () => {
    const state = createChatState();
    state.messages = [user("one", 0), assistant("a"), user("two", 1), assistant("b")];
    const forks = createBranchSnaps();
    expect(
      forks.applyReset(state, {
        user_index: 0,
        sibling_index: 1,
        sibling_count: 2,
        content: "edited",
      }),
    ).toBe(true);
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]?.text).toBe("edited");
    expect(state.messages[0]?.siblingIndex).toBe(1);
    expect(state.messages[0]?.siblingCount).toBe(2);
  });

  it("returns false when the user index is missing", () => {
    const state = createChatState();
    state.messages = [user("one", 0)];
    expect(
      createBranchSnaps().applyReset(state, {
        user_index: 3,
        sibling_index: 0,
        sibling_count: 1,
        content: "nope",
      }),
    ).toBe(false);
    expect(state.messages[0]?.text).toBe("one");
  });
});

describe("applyReset with a snapshot", () => {
  it("restores the sibling instead of truncating the live list", () => {
    const state = createChatState();
    const original = [user("one", 0), assistant("kept")];
    const forks = createBranchSnaps();
    forks.save(0, 0, original);
    state.messages = [user("edited", 0), assistant("other")];
    forks.applyReset(state, {
      user_index: 0,
      sibling_index: 0,
      sibling_count: 2,
      content: "one",
    });
    expect(state.messages.map((m) => m.text)).toEqual(["one", "kept"]);
    expect(state.messages[0]?.siblingIndex).toBe(0);
    expect(state.messages[0]?.siblingCount).toBe(2);
  });
});

describe("saveActive", () => {
  it("refreshes the sibling the live row points at", () => {
    const state = createChatState();
    const forks = createBranchSnaps();
    state.messages = [user("one", 0), assistant("old")];
    forks.save(0, 0, state.messages);
    state.messages = [user("one", 0), assistant("new")];
    forks.saveActive(state);
    state.messages = [];
    forks.applyReset(state, {
      user_index: 0,
      sibling_index: 0,
      sibling_count: 1,
      content: "one",
    });
    expect(state.messages.map((m) => m.text)).toEqual(["one", "new"]);
  });
});
