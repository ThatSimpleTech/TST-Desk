// "Restore files to here" (TD-5125): the dialog's reactive store.
//
// Thin wiring over rewind.ts: one $state the dialog reads, the connection's
// event fan-out in, sendToDaemon out. An error the dialog asked for is shown
// in the dialog and claimed, so no toast repeats it.

import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";
import { chat, forkFrom } from "./chat-store.svelte.js";
import { claimInline } from "./inline-errors";
import {
  closeRewind,
  createRewindState,
  isRewindError,
  openRewind,
  reduceRewind,
  restoreRequest,
  toggleTicked,
  type RewindState,
  type UnsentEdit,
} from "./rewind";

export const rewind: RewindState = $state(createRewindState());

/** Edit and resend's text the dialog closed without sending, waiting for its row. */
export const unsentEdit: { value: UnsentEdit | null } = $state({ value: null });

const NOT_SENT = "Not connected to TST Desk. Try again when it reconnects.";

let started = false;

/** Follow daemon events for the window's life. Returns the unsubscribe. */
export function startRewind(): () => void {
  if (started) return () => {};
  started = true;
  const off = onDaemonEvent((event) => {
    if (isRewindError(rewind, event)) claimInline(event);
    if (reduceRewind(rewind, event) === "fork") resendEdited();
  });
  return () => {
    started = false;
    off();
  };
}

/** Open the dialog on a user row. `resend` is Edit and resend's new text. */
export function openRestore(turnId: string, userIndex: number | null, resend: string | null = null): void {
  const sessionId = chat.sessionId;
  if (sessionId === null) return;
  const ask = openRewind(rewind, sessionId, turnId, userIndex, resend);
  if (!sendToDaemon(ask)) {
    rewind.phase = "error";
    rewind.error = NOT_SENT;
  }
}

/** Restore the files the preview showed, with the ticked hand-edited ones. */
export function confirmRestore(): void {
  const request = restoreRequest(rewind);
  if (request === null) return;
  if (!sendToDaemon(request)) {
    rewind.phase = "error";
    rewind.error = NOT_SENT;
  }
}

/** Edit and resend without touching the files. */
export function resendWithoutRestoring(): void {
  resendEdited();
  closeRestore();
}

export function toggleHandEdited(path: string): void {
  toggleTicked(rewind, path);
}

/** Close the dialog; an edit that never went out goes back to its row. */
export function closeRestore(): void {
  const unsent = closeRewind(rewind);
  if (unsent !== null) unsentEdit.value = unsent;
}

/** The row's edited text handed back by the dialog, once; null when none is waiting. */
export function takeUnsentEdit(turnId: string): string | null {
  const held = unsentEdit.value;
  if (held === null || held.turnId !== turnId) return null;
  unsentEdit.value = null;
  return held.text;
}

function resendEdited(): void {
  if (rewind.resend === null || rewind.userIndex === null) return;
  rewind.resent = forkFrom(rewind.userIndex, rewind.resend);
  // A fork that didn't go out keeps the text, for the row to take back.
  if (rewind.resent) rewind.resend = null;
}
