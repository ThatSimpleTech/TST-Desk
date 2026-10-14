// The new_session in flight (TD-1701, TD-5015).
//
// While an anchor id sits here, the first session_state that isn't the
// attached session's own is the daemon's new_session reply, and the rail store
// focuses it on sight. A state flip on a session owned by another window could
// steal the focus in that window for a few frames; single-window use never
// notices.
//
// The reply can also never come: the hub refuses a computer that isn't
// reachable, the socket drops, or the daemon answers with an error that names
// nothing. Each of those must release the anchor, or every later New is
// refused until a reload. Split from sessions.svelte.ts to keep that file the
// list and the filter.

import { machineOf } from "./bridged-id";
import type { Error as ErrorEvent } from "./protocol";

/** How long a new_session may go unanswered before New works again. Above the
 *  hub's 10 s queue for a lane that is still connecting, after which it
 *  answers machine_unreachable. */
export const NEW_SESSION_REPLY_MS = 15_000;

let anchor: string | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;

/** The anchor of the new_session in flight, or null. */
export function pendingNewAnchor(): string | null {
  return anchor;
}

/** Every write goes through here, so the release timer always matches. */
export function setPendingNew(next: string | null): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
  anchor = next;
  if (next === null) return;
  timer = setTimeout(() => {
    timer = null;
    anchor = null;
  }, NEW_SESSION_REPLY_MS);
}

/** True for an error that answers the pending new_session: one naming the
 *  anchor (the hub's machine_unreachable), one the anchor's computer sent
 *  without a session, or this daemon's own unknown-anchor refusal. */
export function failsPendingNew(event: ErrorEvent): boolean {
  if (anchor === null) return false;
  if (event.session_id === anchor) return true;
  const mch = machineOf(anchor);
  if (mch !== null) return !event.session_id && event.machine === mch;
  return !event.session_id && !event.machine && event.code === "session_not_found";
}
