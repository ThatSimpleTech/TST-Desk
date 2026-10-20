// "Restore files to here" (TD-5125): the dialog's state, kept pure.
//
// One flow at a time: a user row asks the daemon what restoring to that turn
// would undo (`preview_restore_files`), the dialog shows the files and the
// diff, and Restore sends back the preview's `plan_id` with the hand-edited
// files the person ticked. The daemon's `files_restored` closes the loop.
// Edit and resend (AC 3) rides the same flow with the edited text kept in
// `resend`, sent as a fork once the files are back. If it never goes out,
// closing the dialog hands it back to the row's edit box.
//
// The store (rewind-store.svelte.ts) owns the wiring; everything here takes
// the state and an event, so it unit-tests without a socket.

import type {
  DaemonEventUnion,
  FilesRestored,
  FilesRestorePreview,
  PreviewRestoreFiles,
  RestoreFileEntry,
  RestoreFiles,
} from "./protocol";

export type RewindPhase = "closed" | "loading" | "ready" | "restoring" | "done" | "error";

export interface RewindState {
  phase: RewindPhase;
  sessionId: string | null;
  turnId: string | null;
  /** The row's place among user turns, for the resend's fork. */
  userIndex: number | null;
  preview: FilesRestorePreview | null;
  /** Hand-edited files the person ticked to restore anyway. */
  ticked: string[];
  result: FilesRestored | null;
  error: string | null;
  /** Edit and resend: the edited text, forked once the files are back. */
  resend: string | null;
  /** The edited text went out as a fork after the restore. */
  resent: boolean;
}

/** What the reducer asks the store to do next. */
export type RewindAction = "fork" | null;

const VERBS: ReadonlySet<string> = new Set(["preview_restore_files", "restore_files"]);

export function createRewindState(): RewindState {
  return {
    phase: "closed",
    sessionId: null,
    turnId: null,
    userIndex: null,
    preview: null,
    ticked: [],
    result: null,
    error: null,
    resend: null,
    resent: false,
  };
}

/** Open the dialog on one user row; returns the preview request to send. */
export function openRewind(
  state: RewindState,
  sessionId: string,
  turnId: string,
  userIndex: number | null,
  resend: string | null = null,
): PreviewRestoreFiles {
  Object.assign(state, createRewindState(), {
    phase: "loading",
    sessionId,
    turnId,
    userIndex,
    resend,
  });
  return { type: "preview_restore_files", session_id: sessionId, turn_id: turnId };
}

/** Edit and resend's text that never went out, and the row it belongs to. */
export interface UnsentEdit {
  turnId: string;
  text: string;
}

/**
 * Close the dialog. Returns Edit and resend's text when it was not sent
 * (cancelled, refused, or the fork failed), so the row's edit box can take
 * it back instead of losing it.
 */
export function closeRewind(state: RewindState): UnsentEdit | null {
  const unsent =
    state.resend !== null && state.turnId !== null
      ? { turnId: state.turnId, text: state.resend }
      : null;
  Object.assign(state, createRewindState());
  return unsent;
}

/** The restore request for the preview on screen, or null when there is none. */
export function restoreRequest(state: RewindState): RestoreFiles | null {
  const preview = state.preview;
  if (state.phase !== "ready" || preview === null || preview.files.length === 0) return null;
  const handEdited = new Set(preview.files.filter((f) => f.hand_edited).map((f) => f.path));
  state.phase = "restoring";
  return {
    type: "restore_files",
    session_id: preview.session_id,
    turn_id: preview.turn_id,
    plan_id: preview.plan_id,
    include_hand_edited: state.ticked.filter((path) => handEdited.has(path)),
  };
}

export function toggleTicked(state: RewindState, path: string): void {
  state.ticked = state.ticked.includes(path)
    ? state.ticked.filter((p) => p !== path)
    : [...state.ticked, path];
}

/** True for an error this dialog asked for, so no global notice repeats it. */
export function isRewindError(state: RewindState, event: DaemonEventUnion): boolean {
  if (event.type !== "error" || state.phase === "closed") return false;
  if (event.in_reply_to == null || !VERBS.has(event.in_reply_to)) return false;
  return event.session_id == null || event.session_id === state.sessionId;
}

/** Fold one daemon event into the dialog. */
export function reduceRewind(state: RewindState, event: DaemonEventUnion): RewindAction {
  if (isRewindError(state, event) && event.type === "error") {
    if (state.phase === "loading" || state.phase === "restoring") {
      state.phase = "error";
      state.error = event.message;
    }
    return null;
  }
  if (event.type === "files_restore_preview") {
    if (state.phase !== "loading" || !sameTurn(state, event)) return null;
    state.preview = event;
    state.phase = "ready";
    return null;
  }
  if (event.type === "files_restored") {
    if (state.phase !== "restoring" || !sameTurn(state, event)) return null;
    state.result = event;
    state.phase = "done";
    return state.resend !== null ? "fork" : null;
  }
  return null;
}

function sameTurn(state: RewindState, event: { session_id: string; turn_id: string }): boolean {
  return event.session_id === state.sessionId && event.turn_id === state.turnId;
}

/** The files a restore would leave as they are unless ticked. */
export function handEdited(preview: FilesRestorePreview | null): RestoreFileEntry[] {
  return preview === null ? [] : preview.files.filter((f) => f.hand_edited);
}

/** How many files Restore would change, with the ticks as they are. */
export function restoreCount(state: RewindState): number {
  const files = state.preview?.files ?? [];
  return files.filter((f) => !f.hand_edited || state.ticked.includes(f.path)).length;
}

/** The row's verb for what restoring does to one file. */
export function changeLabel(change: RestoreFileEntry["change"]): string {
  switch (change) {
    case "delete":
      return "Delete (the agent made it)";
    case "recreate":
      return "Bring back (the agent deleted it)";
    default:
      return "Undo the agent's changes";
  }
}

function files(n: number): string {
  return n === 1 ? "1 file" : `${n} files`;
}

/** One line for a finished restore. */
export function restoredSummary(result: FilesRestored): string {
  const parts: string[] = [];
  if (result.restored.length > 0) parts.push(`Put back ${files(result.restored.length)}`);
  if (result.removed.length > 0) parts.push(`removed ${files(result.removed.length)}`);
  let line = parts.length === 0 ? "Nothing was changed" : parts.join(", ");
  line += ".";
  if (result.left_alone.length > 0) {
    line += ` Left ${files(result.left_alone.length)} you changed by hand untouched.`;
  }
  if (result.lfs_pointers.length > 0) {
    line += ` ${files(result.lfs_pointers.length)} came back as a git-lfs pointer; run git lfs checkout.`;
  }
  return line;
}
