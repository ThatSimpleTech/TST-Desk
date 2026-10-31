// An older chat's earlier messages in the pane (TD-5201).
//
// An older chat's event log can hold none of its questions, so the replay
// alone showed a fragment, or nothing: the New chat screen for a chat with a
// whole history. Attach now says the log was trimmed and sends what the
// saved conversation still has. These drive the store the way the window's
// event fan-out does.

import { describe, it, expect } from "vitest";
import { createChatState, createChatStore, type ChatDeps } from "./chat-store";
import { noticeCopy } from "./chat-history";
import type { ConversationHistory, DaemonEventUnion, HistoryMessage } from "./protocol";

const SID = "older";

function boundStore() {
  const sent: unknown[] = [];
  const deps: ChatDeps = {
    send: (msg) => {
      sent.push(msg);
      return true;
    },
    attach: () => {},
    detach: () => {},
    drafts: { restore: () => "", schedule: () => {}, flush: () => {} },
  };
  const state = createChatState();
  const store = createChatStore(deps, state);
  store.selectSession(SID, "running");
  return { store, state, sent };
}

function trimmed(sessionId = SID): DaemonEventUnion {
  return { type: "log_trimmed", seq: 1, session_id: sessionId, requested_from_seq: 1, earliest_seq: 500 };
}

function history(
  messages: HistoryMessage[],
  userTurns: number | null,
  truncated = false,
): ConversationHistory {
  return {
    type: "conversation_history",
    seq: 1,
    session_id: SID,
    messages,
    user_turns: userTurns,
    truncated,
  };
}

function user(text: string, index: number): HistoryMessage {
  return { role: "user", text, tools: [], user_index: index, folded_turns: 0 };
}

function reply(text: string, tools: HistoryMessage["tools"] = []): HistoryMessage {
  return { role: "assistant", text, tools, user_index: null, folded_turns: 0 };
}

function delta(text: string, seq: number): DaemonEventUnion {
  return { type: "assistant_delta", session_id: SID, delta: text, seq };
}

function userTurn(text: string, turnId: string, seq: number): DaemonEventUnion {
  return { type: "user_turn", session_id: SID, turn_id: turnId, content: text, seq };
}

function done(seq: number): DaemonEventUnion {
  return {
    type: "turn_complete",
    session_id: SID,
    tokens: 1,
    cost: 0,
    tier: "brain",
    duration: 1,
    failed: false,
    error_code: null,
    seq,
  };
}

function rows(state: ReturnType<typeof boundStore>["state"]): string[] {
  return state.messages.map((m) => `${m.role}${m.notice ? `:${m.notice}` : ""}: ${m.text}`);
}

describe("a replay that starts past the chat's start (TD-5201)", () => {
  it("never leaves the pane empty: it says the earlier messages can't be shown", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ role: "notice", notice: "trimmed" });
    expect(noticeCopy(state.messages[0])).toBe("Earlier messages in this chat can't be shown.");
  });

  it("puts the saved conversation ahead of the replay, in place of that line", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(
      history(
        [
          user("Sketch the beds", 0),
          reply("Checking.", [{ tool_call_id: "c2", name: "read_file", arguments: { path: "notes.md" } }]),
          user("When to water?", 1),
        ],
        2,
      ),
    );
    store.applyEvent(delta("Monday.", 500));
    store.applyEvent(done(501));
    expect(rows(state)).toEqual([
      "user: Sketch the beds",
      "assistant: Checking.",
      "user: When to water?",
      "assistant: Monday.",
    ]);
    const tool = state.messages[1].tools?.[0];
    expect(tool).toMatchObject({ toolCallId: "c2", name: "read_file" });
    expect(tool?.output).toBeUndefined();
    expect(tool?.status).toBeUndefined();
    expect(state.messages[1].parts?.map((p) => p.kind)).toEqual(["text", "tool"]);
    expect(state.messages.slice(0, 3).every((m) => m.restored === true && m.complete)).toBe(true);
  });

  it("numbers the replay's user turns after the history's", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([user("First", 0), reply("One.")], 1));
    store.applyEvent(userTurn("Second", "t2", 500));
    expect(state.messages.at(-1)).toMatchObject({ role: "user", userIndex: 1 });
    store.applyEvent(done(501));
    store.sendUserMessage("Third");
    expect(state.messages.at(-1)).toMatchObject({ role: "user", text: "Third", userIndex: 2 });
  });

  it("numbers from the turns before the window when the history holds no question", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([reply("The end of a long answer.")], 7));
    store.applyEvent(userTurn("Next", "t8", 500));
    expect(state.messages.at(-1)).toMatchObject({ role: "user", userIndex: 7 });
  });

  it("does not take a replayed question for the echo of a restored one", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([user("Continue", 3)], 4));
    store.applyEvent(userTurn("Continue", "t5", 500));
    expect(rows(state)).toEqual(["user: Continue", "user: Continue"]);
    expect(state.messages.map((m) => m.userIndex)).toEqual([3, 4]);
  });

  it("shows a summary's turns as a line, with the summary under it", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(
      history(
        [{ role: "summary", text: "Planned the beds.", tools: [], user_index: null, folded_turns: 3 }, user("Fourth", 3)],
        4,
      ),
    );
    expect(state.messages[0]).toMatchObject({ role: "notice", notice: "summarized", foldedTurns: 3 });
    expect(state.messages[0].text).toBe("Planned the beds.");
    expect(noticeCopy(state.messages[0])).toBe("3 earlier turns were summarized to save room.");
    expect(noticeCopy({ notice: "summarized", foldedTurns: 1 })).toBe(
      "1 earlier turn was summarized to save room.",
    );
  });

  it("says older messages are not shown when the history was cut", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([user("Late ask", 40)], 41, true));
    expect(state.messages[0]).toMatchObject({ role: "notice", notice: "truncated" });
    expect(noticeCopy(state.messages[0])).toBe("Older messages in this chat are not shown.");
  });

  it("drops the line when the history says nothing was lost", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([], 0));
    store.applyEvent(userTurn("Hello", "t1", 500));
    expect(rows(state)).toEqual(["user: Hello"]);
  });

  it("keeps the first copy when a second attach sends the history again", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([user("First", 0)], 1));
    store.applyEvent(delta("Reply.", 500));
    store.applyEvent(trimmed());
    store.applyEvent(history([user("First", 0)], 1));
    expect(rows(state)).toEqual(["user: First", "assistant: Reply."]);
  });

  it("leaves a chat that was never used on the greeting: its log starts at the start", () => {
    const { store, state } = boundStore();
    store.applyEvent({ type: "session_state", session_id: SID, state: "running", seq: 1 });
    store.applyEvent({ type: "session_state", session_id: SID, state: "cancelled", seq: 2 });
    expect(state.messages).toEqual([]);
  });

  it("leaves another session's notice and history alone", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed("someone-else"));
    store.applyEvent({ ...history([user("Not ours", 0)], 1), session_id: "someone-else" });
    expect(state.messages).toEqual([]);
  });

  it("forks a restored turn by its own number (TD-1708)", () => {
    const { store, state, sent } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(
      history(
        [{ role: "summary", text: "", tools: [], user_index: null, folded_turns: 2 }, user("Third", 2), reply("Three.")],
        3,
      ),
    );
    store.applyEvent(userTurn("Fourth", "t4", 500));
    store.applyEvent(done(501));
    expect(store.forkFrom(2, "Third, edited")).toBe(true);
    expect(sent.at(-1)).toMatchObject({ type: "fork_from", user_index: 2 });
    store.applyEvent({
      type: "conversation_reset",
      session_id: SID,
      seq: 502,
      user_index: 2,
      sibling_index: 1,
      sibling_count: 2,
      content: "Third, edited",
    });
    expect(rows(state)).toEqual(["notice:summarized: ", "user: Third, edited"]);
  });

  it("starts the next chat from no history", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([reply("Old.")], 9));
    store.selectSession("fresh", "running");
    expect(state.userTurnBase).toBe(0);
    store.sendUserMessage("Hi");
    expect(state.messages.at(-1)).toMatchObject({ userIndex: 0 });
  });
});

describe("numbers a restored chat's turns the daemon's way (TD-5201)", () => {
  function summary(folded: number): HistoryMessage {
    return { role: "summary", text: "Folded.", tools: [], user_index: null, folded_turns: folded };
  }

  function reset(userIndex: number, content: string, seq: number): DaemonEventUnion {
    return {
      type: "conversation_reset",
      session_id: SID,
      seq,
      user_index: userIndex,
      sibling_index: 1,
      sibling_count: 2,
      content,
    };
  }

  function numbers(state: ReturnType<typeof boundStore>["state"]): (number | undefined)[] {
    return state.messages.filter((m) => m.role === "user").map((m) => m.userIndex);
  }

  it("counts the turns a summary after the last question stands for", () => {
    // "Summarize from here" on turn 1 folded turns 1-4 in place.
    const { store, state, sent } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([user("Plan the beds", 0), reply("Three beds."), summary(4)], 5));
    store.applyEvent(userTurn("Which plants?", "t5", 500));
    store.applyEvent(done(501));
    expect(numbers(state)).toEqual([0, 5]);
    expect(store.retryLastUserMessage()).toBe(true);
    expect(sent.at(-1)).toMatchObject({ type: "fork_from", user_index: 5 });
  });

  it("numbers from the cut once an edit below a summary drops it", () => {
    const { store, state, sent } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(
      history([user("First", 0), reply("One."), user("Second", 1), reply("Two."), summary(3)], 5),
    );
    store.applyEvent(userTurn("Sixth", "t5", 500));
    store.applyEvent(done(501));
    expect(store.forkFrom(1, "Second, edited")).toBe(true);
    store.applyEvent(reset(1, "Second, edited", 502));
    store.applyEvent(userTurn("Second, edited", "t6", 503));
    store.applyEvent(done(504));
    store.sendUserMessage("Next");
    expect(numbers(state)).toEqual([0, 1, 2]);
    expect(sent.at(-1)).toMatchObject({ type: "user_message", content: "Next" });
  });

  it("forks a restored row in place: the fork's own turn adopts it, no second copy", () => {
    const { store, state, sent } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([user("Q0", 0), reply("A0"), user("Q1", 1), reply("A1")], 2));
    expect(store.retryLastUserMessage()).toBe(true);
    expect(sent.at(-1)).toMatchObject({ type: "fork_from", user_index: 1 });
    store.applyEvent(reset(1, "Q1", 500));
    store.applyEvent(userTurn("Q1", "t2", 501));
    store.applyEvent(delta("A1, again.", 502));
    store.applyEvent(done(503));
    expect(rows(state)).toEqual(["user: Q0", "assistant: A0", "user: Q1", "assistant: A1, again."]);
    expect(numbers(state)).toEqual([0, 1]);
    // The row is live now: a second Retry forks the same turn again.
    expect(store.retryLastUserMessage()).toBe(true);
    expect(sent.at(-1)).toMatchObject({ type: "fork_from", user_index: 1 });
  });

  it("numbers no turn when the daemon could not line its history up", () => {
    const { store, state, sent } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([], null));
    expect(state.messages[0]).toMatchObject({ role: "notice", notice: "trimmed" });
    store.applyEvent(userTurn("V1", "t1", 500));
    store.applyEvent(done(501));
    store.applyEvent(userTurn("V2", "t2", 502));
    store.applyEvent(done(503));
    expect(numbers(state)).toEqual([undefined, undefined]);
    // Retry resends as a new message: there is no number to fork by.
    expect(store.retryLastUserMessage()).toBe(true);
    expect(sent.at(-1)).toMatchObject({ type: "user_message", content: "V2" });
    expect(sent.some((m) => (m as { type: string }).type === "fork_from")).toBe(false);
    expect(numbers(state)).toEqual([undefined, undefined, undefined]);
  });

  it("leaves rows it cannot place alone when a replayed fork names a turn", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(history([], null));
    store.applyEvent(userTurn("T5", "t5", 500));
    store.applyEvent(userTurn("T6", "t6", 501));
    store.applyEvent(userTurn("T7", "t7", 502));
    store.applyEvent(done(503));
    store.applyEvent(reset(2, "fork text", 504));
    expect(rows(state)).toEqual([
      "notice:trimmed: ",
      "user: T5",
      "user: T6",
      "user: T7",
    ]);
  });
});
