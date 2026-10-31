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

/** A `fork_from` the daemon has not answered yet (TD-5158). */
export interface PendingFork {
  userIndex: number;
  text: string;
  /** This send took the row's first snapshot, so a refusal drops it. */
  saved: boolean;
}

/** A refused fork's answer is one frame away, so a handful is plenty. */
const MAX_PENDING_FORKS = 8;

export interface BranchSnaps {
  clear(): void;
  has(userIndex: number): boolean;
  save(userIndex: number, sibling: number, messages: readonly ChatMessage[]): void;
  /** A `fork_from` went out (TD-5158). */
  noteFork(fork: PendingFork): void;
  /** The daemon refused the newest fork still waiting: forget the snapshot
   *  it took and hand back what it sent, so the edited text is not lost. */
  takeRefusedFork(): PendingFork | null;
  /** Rebuild the transcript from a reset. True when a user row was found. */
  applyReset(state: ChatState, event: ConversationResetView): boolean;
  /** Refresh the active sibling after a turn lands. */
  saveActive(state: ChatState): void;
}

function cloneMessages(messages: readonly ChatMessage[]): ChatMessage[] {
  return messages.map((m) => {
    // Parts are the order; tools mirror the tool entries with the same
    // objects. Clone parts first, then rebuild the mirror from the clones
    // so the sharing survives the snapshot.
    if (m.parts === undefined) {
      return { ...m, tools: m.tools?.map((t) => ({ ...t })) };
    }
    const parts = m.parts.map((p) =>
      p.kind === "tool" ? { kind: "tool" as const, tool: { ...p.tool } } : { ...p },
    );
    const tools = parts.flatMap((p) => (p.kind === "tool" ? [p.tool] : []));
    return { ...m, parts, tools: tools.length > 0 ? tools : undefined };
  });
}

export function createBranchSnaps(): BranchSnaps {
  const snaps = new Map<string, ChatMessage[][]>();
  const pending: PendingFork[] = [];

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
      pending.length = 0;
    },

    has(userIndex) {
      const list = snaps.get(String(userIndex));
      return list !== undefined && list.length > 0;
    },

    save,

    noteFork(fork) {
      pending.push(fork);
      if (pending.length > MAX_PENDING_FORKS) pending.shift();
    },

    takeRefusedFork() {
      const fork = pending.pop() ?? null;
      if (fork?.saved === true) snaps.delete(String(fork.userIndex));
      return fork;
    },

    applyReset(state, event) {
      // An accepted fork is answered by its reset, not by an error.
      const landed = pending.findIndex((f) => f.userIndex === event.user_index);
      if (landed >= 0) pending.splice(landed, 1);
      const saved = snaps.get(String(event.user_index))?.[event.sibling_index];
      if (saved !== undefined && saved.length > 0) {
        state.messages = saved.map((m) => ({
          ...m,
          siblingIndex: m.userIndex === event.user_index ? event.sibling_index : m.siblingIndex,
          siblingCount: m.userIndex === event.user_index ? event.sibling_count : m.siblingCount,
        }));
        return true;
      }
      // By the row's own number, not its position: a chat whose log no
      // longer reaches its start numbers from the session's start, and one
      // whose numbering is unknown numbers no row (TD-5201), so a reset
      // there finds nothing to cut rather than a row picked by position.
      const cut = state.messages.findIndex(
        (row) => row.role === "user" && row.userIndex === event.user_index,
      );
      if (cut === -1) return false;
      const row = state.messages[cut];
      row.text = event.content;
      row.siblingIndex = event.sibling_index;
      row.siblingCount = event.sibling_count;
      // The row now stands for the fork's turn, not the one it was stamped
      // with, nor the saved message it was restored from. Unstamped, the
      // fork's user_turn adopts it instead of adding a second copy of the
      // same message (TD-5201: a restored row refused it).
      row.turnId = undefined;
      row.restored = undefined;
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
