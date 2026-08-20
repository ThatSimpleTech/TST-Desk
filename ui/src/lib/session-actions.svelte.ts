// Rail row lifecycle commands (TD-1715).
//
// Archive, Delete, and Move to project: what the rail's row affordances send,
// and the small amount of open/confirm state those affordances need. Split
// out of sessions.svelte.ts so the store stays "the daemon's list plus the
// filter" and this file is "what a row can be told to do" — one direction of
// import, store → actions, never back.
//
// Every action is fire-and-listen. The daemon answers each one with a
// refreshed `session_list`, which re-runs the store's reducer and — through
// the chat store's own binding rule — moves the pane off a session that just
// left the default shelf. Nothing here rebinds the pane itself: one authority
// for "which session is bound" is the whole point.
//
// Nothing here decides whether an action is *allowed*, either. Only the
// daemon knows if a turn is in flight, so Delete and Move are always offered
// and a refusal comes back typed (`session_busy`) with its own copy
// (AGENTS §6: the UI never derives truth it wasn't given).

import { sendToDaemon } from "./connection-status.svelte.js";
import { closeRowMenus, sessions } from "./sessions.svelte.js";
import { workspaces } from "./workspaces.svelte.js";

/** Swap the history section between the live shelf and the archived one. */
export function toggleArchivedView(): void {
	sessions.showArchived = !sessions.showArchived;
	closeRowMenus();
}

/** Open (or close) a row's action menu. One row's menu at a time. */
export function toggleRowMenu(sessionId: string): void {
	const wasOpen = sessions.menuFor === sessionId;
	closeRowMenus();
	sessions.menuFor = wasOpen ? null : sessionId;
}

/** File a row away, or restore it.
 *
 *  Offered in every state, including mid-turn: the daemon honours it without
 *  cancelling the turn, which is exactly why Archive is the answer the
 *  Delete/Move refusal points at. */
/** Run distill for a session (TD-2302). Not a kill. */
export function endSession(sessionId: string): boolean {
	const sent = sendToDaemon({ type: "end_session", session_id: sessionId });
	if (sent) closeRowMenus();
	return sent;
}

export function setArchived(sessionId: string, archived: boolean): boolean {
	const sent = sendToDaemon({ type: "archive_session", session_id: sessionId, archived });
	if (sent) closeRowMenus();
	return sent;
}

/** First click on Delete: arm the confirm. Nothing has been sent yet. */
export function requestDelete(sessionId: string): void {
	closeRowMenus();
	sessions.confirmDeleteFor = sessionId;
}

/** Second click: send it. The daemon still gets to refuse, and its refusal
 *  lands in `sessions.refusal` rather than being guessed at here. */
export function confirmDelete(): boolean {
	const sessionId = sessions.confirmDeleteFor;
	if (sessionId === null) return false;
	const sent = sendToDaemon({ type: "delete_session", session_id: sessionId });
	if (sent) sessions.confirmDeleteFor = null;
	return sent;
}

/** Open a row's move-to-project picker. */
export function requestMove(sessionId: string): void {
	closeRowMenus();
	sessions.moveFor = sessionId;
}

/** Where a row can move to: every workspace the app knows, minus the one it
 *  is already in.
 *
 *  Known workspaces come from the recents store, which derives them from the
 *  daemon's own session list — so the picker can only ever offer a project
 *  the daemon has already seen, and the daemon re-validates the target
 *  anyway. */
export function moveTargets(sessionId: string): string[] {
	const row = sessions.rows.find((r) => r.sessionId === sessionId);
	if (row === undefined) return [];
	return workspaces.entries.map((e) => e.path).filter((p) => p !== row.workspacePath);
}

export function moveRow(sessionId: string, workspacePath: string): boolean {
	const sent = sendToDaemon({
		type: "move_session",
		session_id: sessionId,
		workspace_path: workspacePath,
	});
	if (sent) sessions.moveFor = null;
	return sent;
}
