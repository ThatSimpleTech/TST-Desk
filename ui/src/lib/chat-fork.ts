// Conversation siblings (TD-1708).
//
// The event log is append-only and never stored user rows, so a fork cannot
// be another session — attach would lose the original transcript. Sibling
// snapshots live here. The store sends `fork_from` / `set_branch`; this
// module rebuilds `messages` from `conversation_reset`.

import type { ChatMessage, ChatState } from "./chat-store";

export interface ConversationResetView {
  user_index: number;
  sibling_index: number;
  sibling_count: number;
  content: string;
}

export interface BranchSnaps {
  clear(): void;
  has(userIndex: number): boolean;
  save(userIndex: number, sibling: number, messages: readonly ChatMessage[]): void;
  /** Rebuild the transcript from a reset. True when a user row was found. */
  applyReset(state: ChatState, event: ConversationResetView): boolean;
  /** Refresh the active sibling after a turn lands. */
  saveActive(state: ChatState): void;
}

function cloneMessages(messages: readonly ChatMessage[]): ChatMessage[] {
  return messages.map((m) => ({
    ...m,
    tools: m.tools?.map((t) => ({ ...t })),
  }));
}

export function createBranchSnaps(): BranchSnaps {
  const snaps = new Map<string, ChatMessage[][]>();

  function save(userIndex: number, sibling: number, messages: readonly ChatMessage[]): void {
    const key = String(userIndex);
    const list = snaps.get(key) ?? [];
    while (list.length <= sibling) list.push([]);
    list[sibling] = cloneMessages(messages);
    snaps.set(key, list);
  }

  return {
    clear() {
      snaps.clear();
    },

    has(userIndex) {
      const list = snaps.get(String(userIndex));
      return list !== undefined && list.length > 0;
    },

    save,

    applyReset(state, event) {
      const saved = snaps.get(String(event.user_index))?.[event.sibling_index];
      if (saved !== undefined && saved.length > 0) {
        state.messages = saved.map((m) => ({
          ...m,
          siblingIndex: m.userIndex === event.user_index ? event.sibling_index : m.siblingIndex,
          siblingCount: m.userIndex === event.user_index ? event.sibling_count : m.siblingCount,
        }));
        return true;
      }
      let seen = 0;
      let cut = -1;
      for (let i = 0; i < state.messages.length; i++) {
        if (state.messages[i].role !== "user") continue;
        if (seen === event.user_index) {
          cut = i;
          break;
        }
        seen += 1;
      }
      if (cut === -1) return false;
      const row = state.messages[cut];
      row.text = event.content;
      row.siblingIndex = event.sibling_index;
      row.siblingCount = event.sibling_count;
      state.messages.splice(cut + 1);
      save(event.user_index, event.sibling_index, state.messages);
      return true;
    },

    saveActive(state) {
      for (const [key] of snaps) {
        const userIndex = Number(key);
        const row = state.messages.find((m) => m.userIndex === userIndex);
        if (row?.siblingIndex !== undefined) save(userIndex, row.siblingIndex, state.messages);
      }
    },
  };
}
