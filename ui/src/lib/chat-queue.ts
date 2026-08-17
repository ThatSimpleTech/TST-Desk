// The steerable send queue (TD-1704).
//
// Messages composed while a turn holds the loop wait here rather than going
// straight to the daemon. The daemon queues mid-turn user messages too — the
// handler enqueues whenever the session is live and the loop drains one at
// each turn boundary — but that queue is write-only from the client's side:
// the protocol has no message that edits or withdraws a `user_message` once
// sent, and the only way to void one is to kill the session. Rows parked here
// are the only ones a send-now, an edit or a remove can actually reach.
//
// The queue owns its rows and its ids; the store owns the wire and the turn
// lifecycle, and tells this module when a turn ends.

import type { NewAttachment } from "./attachments";

export interface QueuedMessage {
  id: string;
  text: string;
  /** Files staged with the row (TD-1709). They wait here with it: dropping
   *  them at queue time would send a message the user watched go out with
   *  chips attached, minus the chips. */
  attachments: NewAttachment[];
}

/** Whether the queue has any chrome at all. At zero length the container
 *  renders nothing — not a collapsed strip, not an empty-state line. A queue
 *  nobody is using should leave no trace above the composer. */
export function showQueue(queued: readonly QueuedMessage[]): boolean {
  return queued.length > 0;
}

export interface MessageQueue {
  /** Park a message the running turn will not get to yet. */
  add(text: string, attachments?: readonly NewAttachment[]): void;
  /** Hand one row over now, ahead of the rows before it — the steer. */
  sendNow(id: string): boolean;
  /** Replace a row's text in place, keeping its id and its position. */
  edit(id: string, text: string): void;
  /** Drop a row without ever sending it. */
  remove(id: string): void;
  /** Hand the head over now that a turn has ended: one row per turn end, in
   *  order, so everything behind it stays editable while that turn runs. */
  flushHead(): void;
  clear(): void;
}

/** `toWire` returns false when the send did not go out (socket down), in which
 *  case the row stays queued. `turnLive` is the store's single "a turn is in
 *  flight" predicate — the queue deliberately does not invent a second. */
export function createMessageQueue(
  state: { queued: QueuedMessage[] },
  toWire: (text: string, armWait: boolean, attachments: readonly NewAttachment[]) => boolean,
  turnLive: () => boolean,
): MessageQueue {
  // Rows carry their own id space: they are not conversation messages and
  // must never collide with one in a keyed list.
  let nextId = 0;

  function find(id: string): QueuedMessage | undefined {
    return state.queued.find((row) => row.id === id);
  }

  function drop(id: string): void {
    state.queued = state.queued.filter((row) => row.id !== id);
  }

  return {
    add(text: string, attachments: readonly NewAttachment[] = []): void {
      nextId += 1;
      state.queued.push({ id: `q${nextId}`, text, attachments: [...attachments] });
    },

    sendNow(id: string): boolean {
      const row = find(id);
      if (row === undefined) return false;
      // Mid-turn, this must not restage the running turn's shimmer over deltas
      // that are already streaming, so it leaves the first-token clock alone.
      const sent = toWire(row.text, !turnLive(), row.attachments);
      if (sent) drop(id);
      return sent;
    },

    edit(id: string, text: string): void {
      // Stored verbatim, untrimmed: the row is a live edit surface, and
      // rewriting what the user typed under the caret would fight them.
      // Trimming happens once, at the wire.
      const row = find(id);
      if (row !== undefined) row.text = text;
    },

    remove: drop,

    flushHead(): void {
      const head = state.queued[0];
      if (head === undefined) return;
      if (toWire(head.text, true, head.attachments)) state.queued.shift();
    },

    clear(): void {
      state.queued = [];
    },
  };
}
