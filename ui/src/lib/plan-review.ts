// Plan-review model (TD-5123).
//
// Pure, DOM-free: a `plan_review` event reduced to what the card shows,
// the `review_plan` messages the card sends, and the `/plan` command the
// composer handles itself. The runes layer is plan-review-store.svelte.ts.

import type { PlanReview, ReviewPlan } from "./protocol";

/** The daemon refuses a longer plan or edit (`PLAN_MAX_CHARS`). */
export const PLAN_MAX_CHARS = 12_000;

/** A parked `exit_plan_mode` call, as the card shows it. */
export interface PendingPlan {
  sessionId: string;
  toolCallId: string;
  plan: string;
}

export function planFromEvent(event: PlanReview): PendingPlan {
  return { sessionId: event.session_id, toolCallId: event.tool_call_id, plan: event.plan };
}

/**
 * Approve the plan. `edited` is the card's editor text when the person
 * edited it: sent only when it differs, so an untouched plan approves the
 * model's own words.
 */
export function approveMessage(card: PendingPlan, edited: string | null, feedback: string): ReviewPlan {
  const changed = edited !== null && edited.trim() !== card.plan.trim();
  return {
    type: "review_plan",
    session_id: card.sessionId,
    tool_call_id: card.toolCallId,
    decision: "approve",
    plan: changed ? edited.trim() : null,
    feedback: feedback.trim(),
  };
}

/** Reject the plan: plan mode stays on and the feedback goes to the model. */
export function rejectMessage(card: PendingPlan, feedback: string): ReviewPlan {
  return {
    type: "review_plan",
    session_id: card.sessionId,
    tool_call_id: card.toolCallId,
    decision: "reject",
    feedback: feedback.trim(),
  };
}

/** What `/plan` asks for: a toggle, a setting, or plan mode plus a message. */
export type PlanCommand =
  | { kind: "toggle" }
  | { kind: "set"; on: boolean }
  | { kind: "send"; text: string };

/**
 * `/plan` toggles plan mode, `/plan on` and `/plan off` set it, and
 * `/plan <task>` turns it on and sends the task. Null for anything else,
 * which goes to the model as typed.
 */
export function parsePlanCommand(draft: string): PlanCommand | null {
  const match = draft.match(/^\s*\/plan(?:\s+([\s\S]*))?$/i);
  if (match === null) return null;
  const rest = (match[1] ?? "").trim();
  if (rest === "") return { kind: "toggle" };
  const word = rest.toLowerCase();
  if (word === "on" || word === "off") return { kind: "set", on: word === "on" };
  return { kind: "send", text: rest };
}
