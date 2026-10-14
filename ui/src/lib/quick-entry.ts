// Quick-entry session pick (TD-4702).
//
// Pure logic: given a target workspace path and a daemon session_list, choose
// an existing live session or signal that open_workspace is needed.

import { isBridged } from "./bridged-id";
import { isTerminal } from "./session-binding";
import type { SessionSummary } from "./protocol";

export type QuickEntryPick =
	| { action: "attach"; sessionId: string }
	| { action: "open" }
	| { action: "none"; reason: "no_path" };

/** Pick a live session for *path*, or ask the daemon to open the workspace. */
export function pickQuickEntrySession(
	sessions: readonly SessionSummary[],
	path: string | null,
): QuickEntryPick {
	if (path === null || path.trim() === "") {
		return { action: "none", reason: "no_path" };
	}
	const target = path.trim();
	// Quick entry is a local folder: another computer's session in a folder
	// of the same name is never the pick (TD-5015).
	const live = sessions.filter(
		(s) => s.workspace_path === target && !isTerminal(s.state) && !isBridged(s.session_id),
	);
	if (live.length === 0) {
		return { action: "open" };
	}
	const newest = live.reduce((a, b) => (a.updated_at >= b.updated_at ? a : b));
	return { action: "attach", sessionId: newest.session_id };
}
