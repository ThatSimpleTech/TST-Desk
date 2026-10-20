// Reactive plan-review store (TD-5123).
//
// The runes layer over plan-review.ts. A `plan_review` adds a card for the
// bound session; the call's `plan_reviewed` or `tool_result`, or the
// turn's `turn_complete`, takes it off. Same bind-and-clear contract as
// the question cards (TD-5121): a switch drops what the previous session
// left, and the bind's attach replay rebuilds a plan still waiting.
//
// A sent review keeps its card until the daemon says the review ended: a
// refusal (`bad_plan`) comes back to the same card as a notice.

import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";
import { sessionMachineUp } from "./machines.svelte.js";
import { approveMessage, planFromEvent, rejectMessage, type PendingPlan } from "./plan-review";
import type { ReviewPlan } from "./protocol";

export interface PlanCard extends PendingPlan {
  /** A review is on its way and the card waits for the daemon. */
  sent: boolean;
  /** The daemon's refusal of the last review, or why it was not sent. */
  notice: string | null;
}

export const pending = $state<PlanCard[]>([]);

let boundSessionId: string | null = null;
/** The card the last review was sent from: a refusal names the session, not the card. */
let lastSent: string | null = null;

const NOT_SENT = "Not sent: TST Desk is not connected. Try again when it is.";

onDaemonEvent((event) => {
  if (event.type === "plan_review") {
    if (event.session_id !== boundSessionId) return;
    if (indexOf(event.tool_call_id) !== -1) return;
    pending.push({ ...planFromEvent(event), sent: false, notice: null });
  } else if (event.type === "plan_reviewed" || event.type === "tool_result") {
    if (event.session_id !== boundSessionId) return;
    remove(event.tool_call_id);
  } else if (event.type === "turn_complete") {
    // A plan review never outlives its turn.
    if (event.session_id !== boundSessionId) return;
    pending.splice(0, pending.length);
  } else if (event.type === "error" && event.in_reply_to === "review_plan") {
    if (event.session_id != null && event.session_id !== boundSessionId) return;
    const target = lastSent;
    lastSent = null;
    if (target === null) return;
    if (event.code === "no_pending_plan") {
      remove(target);
      return;
    }
    const card = pending.find((c) => c.toolCallId === target);
    if (card !== undefined) {
      card.sent = false;
      card.notice = event.message;
    }
  }
});

/** Point the cards at a session, or at nothing. Same session: no-op. */
export function bindPlanReviews(sessionId: string | null): void {
  if (sessionId === boundSessionId) return;
  boundSessionId = sessionId;
  lastSent = null;
  pending.splice(0, pending.length);
}

function indexOf(toolCallId: string): number {
  return pending.findIndex((c) => c.toolCallId === toolCallId);
}

function remove(toolCallId: string): void {
  const idx = indexOf(toolCallId);
  if (idx !== -1) pending.splice(idx, 1);
}

/** False while the card's session is on a computer that isn't connected. */
export function canReview(card: PlanCard): boolean {
  return sessionMachineUp(card.sessionId);
}

function send(card: PlanCard, build: (live: PlanCard) => ReviewPlan): void {
  const live = pending.find((c) => c.toolCallId === card.toolCallId);
  if (live === undefined || live.sent || !canReview(live)) return;
  if (!sendToDaemon(build(live))) {
    live.notice = NOT_SENT;
    return;
  }
  live.sent = true;
  live.notice = null;
  lastSent = live.toolCallId;
}

/** Approve: plan mode ends with this plan pinned. `edited` is the editor's text, if open. */
export function approvePlan(card: PlanCard, edited: string | null, feedback: string): void {
  send(card, (live) => approveMessage(live, edited, feedback));
}

/** Reject: plan mode stays on and the model hears the feedback. */
export function rejectPlan(card: PlanCard, feedback: string): void {
  send(card, (live) => rejectMessage(live, feedback));
}
