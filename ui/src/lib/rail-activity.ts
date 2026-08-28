// Rail row activity (TD-1720).
//
// The daemon's session `state: "running"` is loop liveness — set once at
// open and spanning the session's whole life (TD-1714). The rail must not
// paint that as "still working". Activity is turn-level: a turn in flight,
// an approval park, or finished.
//
// `busy` is daemon truth from `turn_in_flight` (additive on session_list).
// The bound pane's evidentiary turnState overlays that for the attached
// row so the dot moves with the composer, not the next list refresh.

import type { SessionState, SessionSummary } from "./protocol";
import { isTerminal } from "./session-binding";

/** Display cap for rail / recents names. Storage stays at 60 (TD-3001). */
export const ROW_TITLE_MAX_LEN = 20;

export type RowActivity = "working" | "waiting" | "paused" | "failed" | "finished";

export type ActivityTone = "info" | "warning" | "danger" | "success" | "muted";

/** The fields activity and the title cap read. SessionRow satisfies this. */
export interface ActivityRow {
	sessionId: string;
	state: SessionSummary["state"];
	busy: boolean;
	title: string | null;
}

/** Overlay from the pane that follows one session (TD-1714 evidence). */
export interface BoundTurn {
	sessionId: string | null;
	turnState: SessionState["state"] | null;
	awaitingFirstToken: boolean;
}

export const ACTIVITY_LABELS: Record<RowActivity, string> = {
	working: "Working",
	waiting: "Awaiting approval",
	paused: "Paused",
	failed: "Failed",
	finished: "Finished",
};

/** Dot tone for turn activity — working is accent, not "the session exists". */
export function activityTone(activity: RowActivity): ActivityTone {
	if (activity === "working") return "info";
	if (activity === "waiting" || activity === "paused") return "warning";
	if (activity === "failed") return "danger";
	return "muted";
}

/** Turn activity for a rail row. The bound overlay wins for that session. */
export function rowActivity(row: ActivityRow, bound?: BoundTurn): RowActivity {
	if (bound !== undefined && bound.sessionId === row.sessionId) {
		return activityFromBound(bound, row.state);
	}
	return activityFromRow(row);
}

function activityFromBound(bound: BoundTurn, state: SessionSummary["state"]): RowActivity {
	if (bound.turnState === "awaiting_approval") return "waiting";
	if (bound.turnState === "paused") return "paused";
	if (bound.turnState === "failed") return "failed";
	if (bound.turnState === "running" || bound.awaitingFirstToken) return "working";
	// A null turnState is "no turn" (TD-1714), even if the list still says
	// running. Fall through to the row's parked/terminal state only.
	if (bound.turnState === null || bound.turnState === "idle") {
		if (state === "awaiting_approval") return "waiting";
		if (state === "paused") return "paused";
		if (state === "failed") return "failed";
		return "finished";
	}
	if (isTerminal(bound.turnState)) return "finished";
	return activityFromRow({ sessionId: "", state, busy: false, title: null });
}

function activityFromRow(row: ActivityRow): RowActivity {
	if (row.state === "awaiting_approval") return "waiting";
	if (row.state === "paused") return "paused";
	if (row.state === "failed") return "failed";
	if (row.busy && !isTerminal(row.state)) return "working";
	return "finished";
}

/** Daemon display title, or the short id. Never truncated. */
export function rowTitleFull(row: Pick<ActivityRow, "sessionId" | "title">): string {
	const titled = row.title?.trim();
	return titled ? titled : row.sessionId.slice(0, 8);
}

/** Rail label: full title, capped at ROW_TITLE_MAX_LEN with an ellipsis. */
export function rowTitle(
	row: Pick<ActivityRow, "sessionId" | "title">,
	maxLen: number = ROW_TITLE_MAX_LEN,
): string {
	const full = rowTitleFull(row);
	if (full.length <= maxLen) return full;
	return `${full.slice(0, maxLen)}…`;
}
