// Withdraw a model response the daemon took back (TD-5159).
//
// The provider went quiet partway through a reply. The daemon withdraws that
// response with `assistant_retract` and asks again, so the window shows one
// reply, not the stalled half and then the whole. Every delta of a model
// response carries its `response_id`; the row notes where the latest response
// began, and a retract for that id cuts the row back to the note: its text,
// its thinking and the text parts it opened. Tool calls never arrive inside a
// response (their `tool_call` comes after it ends), so a tool that already ran
// stays on the row. Text with no id (a failure line, a progress note, an older
// daemon) never starts a response and is never withdrawn.

import type { ChatMessage } from "./chat-message";

/** Where one model response began on a row: the lengths it found. */
export interface ResponseMark {
  id: string;
  text: number;
  /** Length of `reasoning`, or null when the row had none. */
  reasoning: number | null;
  reasoningStartedAt: number | undefined;
  reasoningMs: number | undefined;
  parts: number;
  /** Length of the trailing text part when the row ended in text: the
   *  response's text appended to it rather than opening a new one. */
  tail: number | null;
}

/** Note where the response `responseId` begins, at its first delta. */
export function markResponse(row: ChatMessage, responseId: string | null | undefined): void {
  if (responseId === null || responseId === undefined) return;
  if (row.responseMark?.id === responseId) return;
  const parts = row.parts ?? [];
  const last = parts[parts.length - 1];
  row.responseMark = {
    id: responseId,
    text: row.text.length,
    reasoning: row.reasoning === undefined ? null : row.reasoning.length,
    reasoningStartedAt: row.reasoningStartedAt,
    reasoningMs: row.reasoningMs,
    parts: parts.length,
    tail: last !== undefined && last.kind === "text" ? last.text.length : null,
  };
}

/** Cut the newest row that holds `responseId` back to where it began.
 *  False when no row does: the deltas were never seen, or already gone. */
export function withdrawResponse(messages: ChatMessage[], responseId: string): boolean {
  for (let i = messages.length - 1; i >= 0; i--) {
    const row = messages[i];
    if (row.role !== "assistant") continue;
    const mark = row.responseMark;
    if (mark === undefined || mark.id !== responseId) continue;
    cutBack(row, mark);
    return true;
  }
  return false;
}

function cutBack(row: ChatMessage, mark: ResponseMark): void {
  row.text = row.text.slice(0, mark.text);
  if (mark.reasoning === null) {
    delete row.reasoning;
  } else if (row.reasoning !== undefined) {
    row.reasoning = row.reasoning.slice(0, mark.reasoning);
  }
  setOrDelete(row, "reasoningStartedAt", mark.reasoningStartedAt);
  setOrDelete(row, "reasoningMs", mark.reasoningMs);
  const parts = row.parts;
  if (parts !== undefined) {
    parts.splice(mark.parts);
    const last = parts[parts.length - 1];
    if (mark.tail !== null && last !== undefined && last.kind === "text") {
      last.text = last.text.slice(0, mark.tail);
    }
  }
  delete row.responseMark;
}

function setOrDelete(
  row: ChatMessage,
  key: "reasoningStartedAt" | "reasoningMs",
  value: number | undefined,
): void {
  if (value === undefined) delete row[key];
  else row[key] = value;
}
