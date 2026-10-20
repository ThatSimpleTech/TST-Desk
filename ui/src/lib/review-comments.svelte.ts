// Reactive store for the diff view's unsent line comments (TD-5132).
//
// The runes layer over review-comments.ts. Drafts belong to the bound
// session: a switch drops them, the same bind-and-clear contract as the
// plan-review cards. Sending them all is one `submit_line_comments`; the
// drafts stay, marked as sending, until the `user_turn` that carries their
// ids arrives, and a refusal hands them back with the daemon's reason so
// nothing the person wrote is lost. Sent comments are not kept here: the
// Files pane reads them, and their status, from the timeline.

import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";
import { isBridged } from "./bridged-id";
import { toWire, type DraftComment } from "./review-comments";

export const review = $state<{
  drafts: DraftComment[];
  /** Sent, waiting for the daemon to take them as a turn. */
  sending: boolean;
  /** Why the last send did not go, or null. */
  notice: string | null;
}>({ drafts: [], sending: false, notice: null });

let boundSessionId: string | null = null;
let counter = 0;

const NOT_SENT = "Not sent: TST Desk is not connected. Try again when it is.";
const BRIDGED = "Comments can only be sent to a session on this computer.";

onDaemonEvent((event) => {
  if (event.type === "user_turn") {
    if (event.session_id !== boundSessionId || !event.line_comments) return;
    const taken = new Set(event.line_comments.map((c) => c.id));
    const kept = review.drafts.filter((d) => !taken.has(d.id));
    if (kept.length === review.drafts.length) return;
    review.drafts.splice(0, review.drafts.length, ...kept);
    review.sending = false;
  } else if (
    event.type === "error" &&
    event.in_reply_to === "submit_line_comments"
  ) {
    if (event.session_id != null && event.session_id !== boundSessionId) return;
    review.sending = false;
    review.notice = event.message;
  }
});

/** Point the drafts at a session, or at nothing. Same session: no-op. */
export function bindReviewComments(sessionId: string | null): void {
  if (sessionId === boundSessionId) return;
  boundSessionId = sessionId;
  review.drafts.splice(0, review.drafts.length);
  review.sending = false;
  review.notice = null;
}

/** Add a comment on `start`..`end` of `path`; false when it has no text. */
export function addDraft(
  path: string,
  start: number,
  end: number,
  lines: string[],
  body: string,
): boolean {
  if (body.trim() === "" || review.sending) return false;
  counter += 1;
  review.drafts.push({
    id: `lc-${Date.now().toString(36)}-${counter}`,
    path,
    start_line: start,
    end_line: end,
    body,
    lines,
  });
  review.notice = null;
  return true;
}

export function removeDraft(id: string): void {
  if (review.sending) return;
  const at = review.drafts.findIndex((d) => d.id === id);
  if (at !== -1) review.drafts.splice(at, 1);
}

export function discardDrafts(): void {
  if (review.sending) return;
  review.drafts.splice(0, review.drafts.length);
  review.notice = null;
}

/** False for another computer's session: the hub does not forward the verb. */
export function canSendComments(): boolean {
  return boundSessionId !== null && !isBridged(boundSessionId);
}

/** Send every draft as one message. */
export function submitDrafts(): void {
  if (boundSessionId === null || review.drafts.length === 0 || review.sending)
    return;
  if (!canSendComments()) {
    review.notice = BRIDGED;
    return;
  }
  const sent = sendToDaemon({
    type: "submit_line_comments",
    session_id: boundSessionId,
    comments: toWire(review.drafts),
  });
  if (!sent) {
    review.notice = NOT_SENT;
    return;
  }
  review.sending = true;
  review.notice = null;
}
