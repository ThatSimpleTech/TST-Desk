// Question-card model (TD-5121).
//
// Pure, DOM-free: a `question_request` event reduced to what the card
// shows, the person's picks while they answer, and the `answer_question`
// message the card sends. The runes layer is question-store.svelte.ts.

import type { AnswerQuestion, Question, QuestionAnswer, QuestionRequest } from "./protocol";

/** A parked `ask_user` call, as the card shows it. */
export interface PendingQuestion {
  sessionId: string;
  toolCallId: string;
  questions: Question[];
}

/** What the person has chosen so far for one question. */
export interface Draft {
  selected: string[];
  text: string;
}

export function questionFromEvent(event: QuestionRequest): PendingQuestion {
  return {
    sessionId: event.session_id,
    toolCallId: event.tool_call_id,
    questions: event.questions,
  };
}

export function emptyDrafts(pending: PendingQuestion): Draft[] {
  return pending.questions.map(() => ({ selected: [], text: "" }));
}

/** Tap an option: one pick replaces the last unless several are allowed. */
export function toggle(question: Question, draft: Draft, label: string): Draft {
  const picked = draft.selected.includes(label);
  if (question.multi_select) {
    const selected = picked
      ? draft.selected.filter((l) => l !== label)
      : question.options.map((o) => o.label).filter((l) => l === label || draft.selected.includes(l));
    return { ...draft, selected };
  }
  return { ...draft, selected: picked ? [] : [label] };
}

/** A question is answered by a pick or by words of the person's own. */
export function isAnswered(draft: Draft): boolean {
  return draft.selected.length > 0 || draft.text.trim() !== "";
}

/** The daemon's cap on one answer's own words (`QuestionAnswer.text`). */
export const ANSWER_TEXT_MAX = 4000;

/** Words over the cap would be refused with no reply the card could match,
 *  leaving it stuck on "Sending your answer…" (TD-5161). */
export function fitsCap(draft: Draft): boolean {
  return draft.text.trim().length <= ANSWER_TEXT_MAX;
}

export function canSend(drafts: readonly Draft[]): boolean {
  return drafts.length > 0 && drafts.every((d) => isAnswered(d) && fitsCap(d));
}

/** One question, one choice, nothing to type: a tap is the whole answer. */
export function sendsOnTap(pending: PendingQuestion): boolean {
  return pending.questions.length === 1 && !pending.questions[0]!.multi_select;
}

export function answerMessage(pending: PendingQuestion, drafts: readonly Draft[]): AnswerQuestion {
  const answers: QuestionAnswer[] = drafts.map((d) => ({ selected: [...d.selected], text: d.text.trim() }));
  return {
    type: "answer_question",
    session_id: pending.sessionId,
    tool_call_id: pending.toolCallId,
    answers,
  };
}

export function declineMessage(pending: PendingQuestion): AnswerQuestion {
  return {
    type: "answer_question",
    session_id: pending.sessionId,
    tool_call_id: pending.toolCallId,
    answers: [],
    declined: true,
  };
}
