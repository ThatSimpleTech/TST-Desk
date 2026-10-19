// Messages a restart left held (TD-5155).
//
// A message the daemon accepted is the daemon's to keep (TD-5019): one it
// had queued when it stopped comes back in `queued_messages`, not as a turn.
// These rows are the daemon's, not the pane's (chat-queue.ts holds the
// pane's own): the pane shows the latest list whole, and Send or Remove asks
// the daemon, which answers with the next list. Nothing here sends by itself.

import type { ChatMessage } from "./chat-message";
import { takeSentText } from "./draft-store";
import type { QueuedMessages } from "./protocol";

export interface HeldMessage {
  id: string;
  text: string;
  /** Attached images the daemon did not keep through the restart. */
  imagesDropped: number;
  /** Send or Remove went out and the daemon's next list has not come back. */
  pending: boolean;
}

/** The note under a row that lost its images, or null. */
export function droppedNote(imagesDropped: number): string | null {
  if (imagesDropped <= 0) return null;
  return imagesDropped === 1
    ? "1 attached image was not kept and will not be sent"
    : `${imagesDropped} attached images were not kept and will not be sent`;
}

/** Take the daemon's list. Every list is the daemon's answer, so no row
 *  stays pending past it: one it still names (a Send lost in a restart, say)
 *  can be clicked again. A send of ours that the daemon held loses its local
 *  echo from the transcript, since the held row is where it now is; the
 *  echo is the text as typed, which an attachment's file block follows in
 *  the daemon's copy. */
export function applyHeldList(
  state: { held: HeldMessage[]; messages: ChatMessage[] },
  sending: string[],
  event: QueuedMessages,
): void {
  state.held = event.messages.map((entry) => ({
    id: entry.queued_id,
    text: entry.content,
    imagesDropped: entry.images_dropped,
    pending: false,
  }));
  for (const entry of event.messages) {
    const sent = takeSentText(sending, entry.content);
    if (sent === null) continue;
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const row = state.messages[i];
      if (row.role === "user" && row.turnId === undefined && row.text === sent) {
        state.messages.splice(i, 1);
        break;
      }
    }
  }
}

/** A refused Send or Remove leaves its row as it was, ready to try again. */
export function clearHeldPending(held: HeldMessage[]): void {
  for (const row of held) row.pending = false;
}
