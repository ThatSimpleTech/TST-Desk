// An older chat's earlier messages (TD-5201).
//
// The pane rebuilds from the session's event log, which keeps its newest
// events only: every streamed chunk is one, so an older chat's log can hold
// none of its questions. Attach then says so with `log_trimmed`, and sends
// what the saved conversation still has as `conversation_history`. These
// put that in front of the replay, or say plainly that it cannot be shown,
// instead of showing a chat with a history as the New chat screen.

import type { ChatMessage, NoticeKind, ToolBlock } from "./chat-message";
import type { ChatState } from "./chat-store";
import type { ConversationHistory, HistoryMessage } from "./protocol";

function noticeRow(id: string, notice: NoticeKind, text = "", foldedTurns?: number): ChatMessage {
  return { id, role: "notice", notice, text, complete: true, at: Date.now(), foldedTurns };
}

/** The window about to replay does not reach back to the chat's start. In an
 *  empty pane, say so; a history that arrives next takes the line's place. */
export function noteTrimmed(state: ChatState, allocateId: () => string): void {
  if (state.messages.length > 0) return;
  state.messages.push(noticeRow(allocateId(), "trimmed"));
}

/** Rows from the saved conversation, ahead of the replay. Only into a pane
 *  holding nothing but that line: a duplicate attach sends a second copy,
 *  and the first one already stands there. A history the daemon could not
 *  line up leaves the line, and numbers no turn from here on: Edit and
 *  Retry fork by the number, and a guessed one forks the wrong turn. */
export function applyHistory(
  state: ChatState,
  event: ConversationHistory,
  allocateId: () => string,
): void {
  if (state.messages.some((m) => m.role !== "notice")) return;
  state.userTurnBase = event.user_turns;
  if (event.user_turns === null) {
    if (state.messages.length === 0) state.messages.push(noticeRow(allocateId(), "trimmed"));
    return;
  }
  const rows: ChatMessage[] = [];
  if (event.truncated) rows.push(noticeRow(allocateId(), "truncated"));
  for (const message of event.messages) rows.push(historyRow(message, allocateId()));
  state.messages = rows;
}

function historyRow(message: HistoryMessage, id: string): ChatMessage {
  if (message.role === "summary") {
    return noticeRow(id, "summarized", message.text, message.folded_turns);
  }
  if (message.role === "user") {
    return {
      id,
      role: "user",
      text: message.text,
      complete: true,
      at: Date.now(),
      restored: true,
      userIndex: message.user_index ?? undefined,
      siblingIndex: 0,
      siblingCount: 1,
    };
  }
  // The daemon says what each call was, not what it returned or whether it
  // failed: the block carries no status, and a finished row never shimmers.
  // A result the replay still holds fills it in.
  const tools: ToolBlock[] = message.tools.map((tool) => ({
    toolCallId: tool.tool_call_id,
    name: tool.name,
    arguments: tool.arguments,
  }));
  return {
    id,
    role: "assistant",
    text: message.text,
    complete: true,
    at: Date.now(),
    restored: true,
    tools: tools.length > 0 ? tools : undefined,
    parts: [
      ...(message.text === "" ? [] : [{ kind: "text" as const, text: message.text }]),
      ...tools.map((tool) => ({ kind: "tool" as const, tool })),
    ],
  };
}

/** The number the next user row takes (TD-1708): one past the last numbered
 *  user row and the turns any summary after it stands for (Summarize from
 *  here folds turns in place), or, with none yet, the turns the history said
 *  come before the window. None while the numbering is unknown. */
export function nextUserIndex(state: ChatState): number | undefined {
  if (state.userTurnBase === null) return undefined;
  let folded = 0;
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const row = state.messages[i];
    if (row.role === "user" && row.userIndex !== undefined) return row.userIndex + 1 + folded;
    if (row.notice === "summarized") folded += row.foldedTurns ?? 0;
  }
  return state.userTurnBase;
}

/** What a notice row says. The summary's own text sits behind its fold. */
export function noticeCopy(row: Pick<ChatMessage, "notice" | "foldedTurns">): string {
  if (row.notice === "summarized") {
    const turns = row.foldedTurns ?? 0;
    return `${turns} earlier ${turns === 1 ? "turn was" : "turns were"} summarized to save room.`;
  }
  if (row.notice === "truncated") return "Older messages in this chat are not shown.";
  return "Earlier messages in this chat can't be shown.";
}
