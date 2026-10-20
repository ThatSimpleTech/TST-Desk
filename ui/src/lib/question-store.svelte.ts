// Reactive question-card store (TD-5121).
//
// The runes layer over question.ts. A `question_request` adds a card for
// the bound session; the call's `question_answered` or `tool_result`, or
// the turn's `turn_complete` (a restart seals a cut turn that way), takes
// it off. Same bind-and-clear contract as the approval cards (TD-1014): a
// switch drops what the previous session left, and the bind's attach
// replay rebuilds a question still waiting.
//
// Unlike an approval, a sent answer keeps its card until the daemon says
// the question ended: a `bad_answer` refusal comes back to the same card
// as a notice and the person fixes it there.

import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";
import { sessionMachineUp } from "./machines.svelte.js";
import { answerMessage, declineMessage, questionFromEvent, type Draft, type PendingQuestion } from "./question";

export interface QuestionCard extends PendingQuestion {
  /** An answer is on its way and the card waits for the daemon. */
  sent: boolean;
  /** The daemon's refusal of the last answer, or why it was not sent. */
  notice: string | null;
}

export const pending = $state<QuestionCard[]>([]);

let boundSessionId: string | null = null;
/** The card the last answer was sent from: a refusal names the session, not the card. */
let lastSent: string | null = null;

const NOT_SENT = "Not sent: TST Desk is not connected. Try again when it is.";

onDaemonEvent((event) => {
  if (event.type === "question_request") {
    if (event.session_id !== boundSessionId) return;
    if (indexOf(event.tool_call_id) !== -1) return;
    pending.push({ ...questionFromEvent(event), sent: false, notice: null });
  } else if (event.type === "question_answered" || event.type === "tool_result") {
    if (event.session_id !== boundSessionId) return;
    remove(event.tool_call_id);
  } else if (event.type === "turn_complete") {
    // A question never outlives its turn.
    if (event.session_id !== boundSessionId) return;
    pending.splice(0, pending.length);
  } else if (event.type === "error" && event.in_reply_to === "answer_question") {
    if (event.session_id != null && event.session_id !== boundSessionId) return;
    const target = lastSent;
    lastSent = null;
    if (target === null) return;
    if (event.code === "no_pending_question") {
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

/** Point the cards at a session, or at nothing (TD-1014). Same session: no-op. */
export function bindQuestions(sessionId: string | null): void {
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
export function canAnswer(card: QuestionCard): boolean {
  return sessionMachineUp(card.sessionId);
}

function send(card: QuestionCard, build: (live: QuestionCard) => ReturnType<typeof declineMessage>): void {
  const live = pending.find((c) => c.toolCallId === card.toolCallId);
  if (live === undefined || live.sent || !canAnswer(live)) return;
  if (!sendToDaemon(build(live))) {
    live.notice = NOT_SENT;
    return;
  }
  live.sent = true;
  live.notice = null;
  lastSent = live.toolCallId;
}

/** Send the person's answers, one per question. */
export function answer(card: QuestionCard, drafts: readonly Draft[]): void {
  send(card, (live) => answerMessage(live, drafts));
}

/** Skip every question: the model hears the person chose not to answer. */
export function decline(card: QuestionCard): void {
  send(card, declineMessage);
}
