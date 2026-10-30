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
  userTurns: number,
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
  it("never leaves the pane empty: it says the earlier messages are gone", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ role: "notice", notice: "trimmed" });
    expect(noticeCopy(state.messages[0])).toBe("Earlier messages in this chat are no longer stored.");
  });

  it("puts the saved conversation ahead of the replay, in place of that line", () => {
    const { store, state } = boundStore();
    store.applyEvent(trimmed());
    store.applyEvent(
      history(
        [
          user("Sketch the beds", 0),
          reply("Checking.", [
            { tool_call_id: "c2", name: "read_file", arguments: { path: "notes.md" }, output: "basil" },
          ]),
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
    expect(tool).toMatchObject({ toolCallId: "c2", name: "read_file", output: "basil" });
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
