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
//
// Send now mid-turn steers (TD-5112): the daemon hands the message to the
// running turn at its next tool-batch boundary. Until its `user_turn` comes
// back the row stays here, marked waiting and no longer editable, because
// it is the daemon's now. It reaches the transcript at the point the model
// read it, not where the click happened.

import type { NewAttachment } from "./attachments";

export interface QueuedMessage {
  id: string;
  text: string;
  /** Files staged with the row (TD-1709). They wait here with it: dropping
   *  them at queue time would send a message the user watched go out with
   *  chips attached, minus the chips. */
  attachments: NewAttachment[];
  /** Sent with Send now while a turn ran (TD-5112): the daemon has it and
   *  hands it over at the next step. Read-only until its user_turn. */
  waiting?: boolean;
}

/** Whether the queue has any chrome at all. At zero length the container
 *  renders nothing — not a collapsed strip, not an empty-state line. A queue
 *  nobody is using should leave no trace above the composer. */
export function showQueue(queued: readonly QueuedMessage[]): boolean {
  return queued.length > 0;
}

/** How a message goes out (TD-5112). `turn` starts the next turn: echoed in
 *  the transcript and timed to its first token. `steer` is Send now while a
 *  turn runs: the daemon hands it to that turn at its next step, and its
 *  user_turn places it then. `behind` is Send now of files with no text
 *  while a turn runs: their user_turn cannot be told from another device's,
 *  so they are echoed now and wait for a turn of their own, as before. */
export type SendMode = "turn" | "steer" | "behind";

/** What a waiting row says (TD-5112). The native loop takes it at the next
 *  tool-batch boundary. The Grok engine runs its own tools, so there it
 *  waits for the turn to end. */
export function waitingLabel(engine: "native" | "grok" | null | undefined): string {
  return engine === "grok" ? "Sent, waiting for this turn to end" : "Sent, waiting for the next step";
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
   *  order, so everything behind it stays editable while that turn runs.
   *  Not while a sent row waits: the daemon owes that one a turn first. */
  flushHead(): void;
  /** A waiting row's user_turn arrived, or the daemon held or refused it: it
   *  leaves, and is returned so its transcript row can keep the chips it was
   *  sent with. */
  settle(text: string): QueuedMessage | undefined;
  /** Whether any sent row still waits for the daemon to take it. */
  hasWaiting(): boolean;
  /** The session ended with rows still waiting: no turn will take them, so
   *  they are ordinary rows again, to copy or remove. */
  release(): void;
  clear(): void;
}

/** `toWire` returns false when the send did not go out (socket down), in which
 *  case the row stays queued. `turnLive` is the store's single "a turn is in
 *  flight" predicate — the queue deliberately does not invent a second. */
export function createMessageQueue(
  state: { queued: QueuedMessage[] },
  toWire: (text: string, mode: SendMode, attachments: readonly NewAttachment[]) => boolean,
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

  function waitingRow(text: string): QueuedMessage | undefined {
    return state.queued.find((row) => row.waiting === true && row.text === text);
  }

  return {
    add(text: string, attachments: readonly NewAttachment[] = []): void {
      nextId += 1;
      state.queued.push({ id: `q${nextId}`, text, attachments: [...attachments] });
    },

    sendNow(id: string): boolean {
      const row = find(id);
      if (row === undefined || row.waiting === true) return false;
      if (!turnLive() || row.text.trim() === "") {
        const sent = toWire(row.text, turnLive() ? "behind" : "turn", row.attachments);
        if (sent) drop(id);
        return sent;
      }
      // Mid-turn it steers (TD-5112). No echo and no new first-token clock:
      // the running turn's shimmer stays, and the row waits here until the
      // daemon says where the model read it.
      const sent = toWire(row.text, "steer", row.attachments);
      if (sent) {
        row.text = row.text.trim();
        row.waiting = true;
      }
      return sent;
    },

    edit(id: string, text: string): void {
      // Stored verbatim, untrimmed: the row is a live edit surface, and
      // rewriting what the user typed under the caret would fight them.
      // Trimming happens once, at the wire.
      const row = find(id);
      if (row !== undefined && row.waiting !== true) row.text = text;
    },

    remove(id: string): void {
      // A waiting row is the daemon's: the protocol has no way to take it back.
      if (find(id)?.waiting !== true) drop(id);
    },

    flushHead(): void {
      if (state.queued.some((row) => row.waiting === true)) return;
      const head = state.queued[0];
      if (head === undefined) return;
      if (toWire(head.text, "turn", head.attachments)) state.queued.shift();
    },

    settle(text: string): QueuedMessage | undefined {
      const row = waitingRow(text);
      if (row !== undefined) drop(row.id);
      return row;
    },

    hasWaiting(): boolean {
      return state.queued.some((row) => row.waiting === true);
    },

    release(): void {
      for (const row of state.queued) row.waiting = false;
    },

    clear(): void {
      state.queued = [];
    },
  };
}
