// Unsent text survives a restart (TD-5019).
//
// Text the daemon has not taken lives only in this webview: the composer
// draft, the queued rows (chat-queue.ts), and a send still on its way. A
// window close, an app restart or a daemon restart used to lose all three.
// This module keeps the first two per session in localStorage, and tracks
// the third so a send the daemon refuses can come back to the composer.
//
// Plain text on this device only. Attachment bytes are never stored, and a
// queued row comes back as draft text, never as a send: nothing the user did
// not click goes out (AGENTS §2.3). Every storage access is guarded, so the
// pane works the same with storage missing, full or throwing.

import type { QueuedMessage } from "./chat-queue";
import type { ChatState } from "./chat-store";

export const DRAFT_KEY_PREFIX = "tst.draft.v1.";

/** Quiet time before a save: a write per keystroke would be one per keystroke. */
export const DRAFT_SAVE_DELAY_MS = 300;

export function draftKey(sessionId: string): string {
  return `${DRAFT_KEY_PREFIX}${sessionId}`;
}

/** What one session keeps: the draft verbatim, and the text-only queued rows. */
interface SavedDraft {
  text: string;
  queued: string[];
}

export interface DraftStore {
  /** Save once DRAFT_SAVE_DELAY_MS pass without another call. The values
   *  are read now, so an effect calling this tracks what it saves. */
  schedule(sessionId: string, draft: string, queued: readonly QueuedMessage[]): void;
  /** Save now, replacing any save still waiting for this session. */
  saveNow(sessionId: string, draft: string, queued: readonly QueuedMessage[]): void;
  /** The composer text a session comes back with: its queued rows, then its
   *  draft, a blank line apart. "" when nothing was kept or it can't be read. */
  restore(sessionId: string): string;
}

function browserStorage(): Storage | null {
  return typeof localStorage === "undefined" ? null : localStorage;
}

function snapshot(draft: string, queued: readonly QueuedMessage[]): SavedDraft {
  return {
    text: draft,
    queued: queued.filter((row) => row.attachments.length === 0 && row.text.trim() !== "").map((row) => row.text),
  };
}

function isSavedDraft(value: unknown): value is SavedDraft {
  if (typeof value !== "object" || value === null) return false;
  const { text, queued } = value as Record<string, unknown>;
  return typeof text === "string" && Array.isArray(queued) && queued.every((row) => typeof row === "string");
}

/** `storage` is a getter, not a Storage: reading `localStorage` itself
 *  throws where storage is disabled, and that throw belongs in the guard. */
export function createDraftStore(storage: () => Storage | null = browserStorage): DraftStore {
  let pending: { sessionId: string; saved: SavedDraft; timer: ReturnType<typeof setTimeout> } | null = null;

  function write(sessionId: string, saved: SavedDraft): void {
    try {
      const store = storage();
      if (store === null) return;
      if (saved.text.trim() === "" && saved.queued.length === 0) {
        store.removeItem(draftKey(sessionId));
      } else {
        store.setItem(draftKey(sessionId), JSON.stringify(saved));
      }
    } catch {
      // Storage unavailable or full: the text stays in the composer, it
      // just won't outlive this window. Not worth a notice of its own.
    }
  }

  function flush(): void {
    if (pending === null) return;
    clearTimeout(pending.timer);
    const { sessionId, saved } = pending;
    pending = null;
    write(sessionId, saved);
  }

  /** Settle a save waiting for another session before touching this one. */
  function settle(sessionId: string): void {
    if (pending === null) return;
    if (pending.sessionId !== sessionId) {
      flush();
      return;
    }
    clearTimeout(pending.timer);
    pending = null;
  }

  return {
    schedule(sessionId, draft, queued) {
      const saved = snapshot(draft, queued);
      settle(sessionId);
      pending = { sessionId, saved, timer: setTimeout(flush, DRAFT_SAVE_DELAY_MS) };
    },

    saveNow(sessionId, draft, queued) {
      settle(sessionId);
      write(sessionId, snapshot(draft, queued));
    },

    restore(sessionId) {
      if (pending?.sessionId === sessionId) flush();
      let saved: unknown;
      try {
        const raw = storage()?.getItem(draftKey(sessionId)) ?? null;
        saved = raw === null ? null : JSON.parse(raw);
      } catch {
        return "";
      }
      if (!isSavedDraft(saved)) return "";
      return [...saved.queued.map((row) => row.trim()), saved.text].filter((part) => part.trim() !== "").join("\n\n");
    },
  };
}

// ── Sends on their way ─────────────────────────────────────────────────────
//
// The daemon answers a refused send with an error naming only the session,
// and an accepted one with nothing until its user_turn. So the store keeps
// the texts it sent until one of those two comes back (TD-5023).

/** Each answer is one event away, so a handful covers any burst. The cap
 *  only bounds what an echo that never matched would leave behind. */
const MAX_SENDING = 8;

/** A send went to the daemon. Attachment-only sends carry no text to give
 *  back, so they are not tracked. */
export function noteSent(sending: string[], text: string): void {
  if (text === "") return;
  sending.push(text);
  if (sending.length > MAX_SENDING) sending.shift();
}

/** The daemon started a turn with `content`. True when it was one of ours.
 *  The daemon folds a text attachment into the message after a blank line,
 *  so that echo starts with what was sent rather than equalling it. */
export function takeSent(sending: string[], content: string): boolean {
  const index = sending.findIndex((text) => content === text || content.startsWith(`${text}\n\n`));
  if (index < 0) return false;
  sending.splice(index, 1);
  return true;
}

/** The daemon refused a send: the newest text still on its way. An accepted
 *  send can wait unanswered for a whole turn (a send-now sits in the
 *  daemon's queue), so the oldest is the wrong guess. A drain refuses every
 *  send, and taking the newest each time while `returnToDraft` puts it in
 *  front gives the texts back in the order they were sent. */
export function refusedSend(sending: string[]): string | null {
  return sending.pop() ?? null;
}

/** Put a refused text back in the composer, ahead of anything typed since,
 *  and take its echo row out of the transcript: it was never sent. */
export function returnToDraft(state: Pick<ChatState, "draft" | "messages">, text: string): void {
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const row = state.messages[i];
    if (row.role === "user" && row.turnId === undefined && row.text === text) {
      state.messages.splice(i, 1);
      break;
    }
  }
  state.draft = state.draft.trim() === "" ? text : `${text}\n\n${state.draft}`;
}
