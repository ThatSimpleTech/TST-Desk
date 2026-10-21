// Reactive store for the diff view's unsent line comments (TD-5132).
//
// The runes layer over review-comments.ts. Drafts belong to the bound
// session: a switch drops them, the same bind-and-clear contract as the
// plan-review cards. Sending them all is one `submit_line_comments`.
//
// A send never locks the pane. The daemon queues the message and only logs
// its `user_turn` when the loop takes it, which for comments sent mid-turn
// is when that turn ends, and a held message may never become a turn at
// all. So a send moves the drafts out of the editable list at once and keeps
// a copy, shown as waiting, until the `user_turn` that carries their ids
// arrives. A refusal (an `error` naming `in_reply_to` and the refused
// `comment_ids`, which the daemon sends straight back) hands those back as
// drafts with the daemon's reason, so
// nothing the person wrote is lost, and so does the session ending while
// they wait. A held queue (`queued_messages`) now owns
// the message, so the copy is dropped there too. Sent comments are not kept
// here: the Files pane reads them, and their status, from the timeline.

import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";
import { isBridged } from "./bridged-id";
import { toWire, type DraftComment } from "./review-comments";

export const review = $state<{
  drafts: DraftComment[];
  /** Sent and not yet taken as a turn: shown as waiting, not editable. */
  waiting: DraftComment[];
  /** Why the last send did not go, or null. */
  notice: string | null;
}>({ drafts: [], waiting: [], notice: null });

let boundSessionId: string | null = null;
let counter = 0;

const NOT_SENT = "Not sent: TST Desk is not connected. Try again when it is.";
const BRIDGED = "Comments can only be sent to a session on this computer.";
const SESSION_ENDED =
  "The session ended before the agent read these comments. They are still here to copy.";
/** Session states no turn starts from again (the daemon's TERMINAL_STATES). */
const ENDED = new Set(["complete", "failed", "cancelled", "interrupted"]);

onDaemonEvent((event) => {
  if (event.type === "user_turn") {
    if (event.session_id !== boundSessionId || !event.line_comments) return;
    const taken = new Set(event.line_comments.map((c) => c.id));
    const kept = review.waiting.filter((d) => !taken.has(d.id));
    if (kept.length !== review.waiting.length)
      review.waiting.splice(0, review.waiting.length, ...kept);
  } else if (event.type === "queued_messages") {
    // The session holds its queue for the person to send or remove: the
    // held-message card now owns these comments' message.
    if (event.session_id === boundSessionId && event.messages.length > 0)
      review.waiting.splice(0, review.waiting.length);
  } else if (event.type === "session_state") {
    // Ended with the comments still queued: no turn will take them.
    if (event.session_id !== boundSessionId || !ENDED.has(event.state)) return;
    if (review.waiting.length === 0) return;
    const back = review.waiting.splice(0, review.waiting.length);
    review.drafts.splice(0, 0, ...back);
    review.notice = SESSION_ENDED;
  } else if (
    event.type === "error" &&
    event.in_reply_to === "submit_line_comments"
  ) {
    if (event.session_id != null && event.session_id !== boundSessionId) return;
    // Hand the refused comments back ahead of any written since. A refusal
    // names its comments, so ones an earlier send queued stay waiting (and
    // are not sent twice); an older daemon's refusal hands back them all.
    const refused = event.comment_ids ? new Set(event.comment_ids) : null;
    const back = review.waiting.filter((d) => refused === null || refused.has(d.id));
    if (back.length === 0) return;
    const kept = review.waiting.filter((d) => !back.includes(d));
    review.waiting.splice(0, review.waiting.length, ...kept);
    review.drafts.splice(0, 0, ...back);
    review.notice = event.message;
  }
});

/** Point the drafts at a session, or at nothing. Same session: no-op. */
export function bindReviewComments(sessionId: string | null): void {
  if (sessionId === boundSessionId) return;
  boundSessionId = sessionId;
  review.drafts.splice(0, review.drafts.length);
  review.waiting.splice(0, review.waiting.length);
  review.notice = null;
}

/** Add a comment on `start`..`end` of `path`, made on the diff of the write
 *  logged at `baseSeq`; false when it has no text. */
export function addDraft(
  path: string,
  start: number,
  end: number,
  lines: string[],
  body: string,
  baseSeq: number | null = null,
): boolean {
  if (body.trim() === "") return false;
  counter += 1;
  review.drafts.push({
    id: `lc-${Date.now().toString(36)}-${counter}`,
    path,
    start_line: start,
    end_line: end,
    body,
    lines,
    base_seq: baseSeq,
  });
  review.notice = null;
  return true;
}

export function removeDraft(id: string): void {
  const at = review.drafts.findIndex((d) => d.id === id);
  if (at !== -1) review.drafts.splice(at, 1);
}

export function discardDrafts(): void {
  review.drafts.splice(0, review.drafts.length);
  review.notice = null;
}

/** False for another computer's session: the hub does not forward the verb. */
export function canSendComments(): boolean {
  return boundSessionId !== null && !isBridged(boundSessionId);
}

/** Send every draft as one message. */
export function submitDrafts(): void {
  if (boundSessionId === null || review.drafts.length === 0) return;
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
  const moved = review.drafts.splice(0, review.drafts.length);
  review.waiting.push(...moved);
  review.notice = null;
}
