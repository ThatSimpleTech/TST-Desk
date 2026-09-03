// OS notification copy (TD-1702).
//
// Pure: given a daemon event, either there is something the OS should say
// or there is not. Focus and permission live in the store — this file
// does not know about Tauri.

import type { DaemonEventUnion, JobEntry } from "./protocol";
import { redact } from "./redact";

export interface OsNotice {
	title: string;
	body: string;
	kind: "approval" | "turn" | "scheduled";
	sessionId?: string;
	toolCallId?: string;
	/** Class B approvals can Approve/Deny from the banner. Class C must open the card. */
	actions?: ReadonlyArray<{ id: "approve" | "deny"; title: string }>;
}

/** What the OS should say for this event, or null if it is not a notify. */
export function noticeFor(event: DaemonEventUnion): OsNotice | null {
	if (event.type === "approval_request") {
		const classB = event.decision_class === "B";
		return {
			title: "Approval needed",
			// The daemon redacts at event-log insertion; this is the same belt
			// the diagnostics report gets — a notification banner is a human-
			// facing surface too (TD-4801).
			body: redact(event.summary) || `The agent wants to run ${event.tool_name}`,
			kind: "approval",
			sessionId: event.session_id,
			toolCallId: event.tool_call_id,
			actions: classB
				? [
						{ id: "approve", title: "Approve" },
						{ id: "deny", title: "Deny" },
					]
				: undefined,
		};
	}
	if (event.type === "turn_complete") {
		return {
			title: event.failed ? "Turn failed" : "Turn complete",
			body: event.failed
				? redact(event.error_code ?? "The turn did not finish")
				: "The agent finished a turn",
			kind: "turn",
		};
	}
	return null;
}

/** A banner is a glance, not a transcript. The daemon caps a run summary
 *  at 2000 characters; that is a paragraph too many for a notification. */
const _BANNER_CHARS = 200;

function banner(text: string): string {
	const clean = redact(text).trim();
	if (clean.length <= _BANNER_CHARS) return clean;
	return `${clean.slice(0, _BANNER_CHARS - 1).trimEnd()}…`;
}

/** `last_run` per job id, "" for a job that has never fired. */
export function runStamps(jobs: readonly JobEntry[]): Map<string, string> {
	return new Map(jobs.map((job) => [job.id, job.last_run ?? ""]));
}

/**
 * Notices for scheduled runs that finished since `before` (TD-3807).
 *
 * A scheduled job runs precisely when nobody is watching, so stamping the
 * receipt on a pane is not delivery — this is what makes `deliver_to:
 * "window"` mean anything while the window is in the background. The other
 * channels deliver themselves and are left alone here, or every slack job
 * would arrive twice.
 *
 * A job absent from `before` never notifies: it may carry a run from before
 * this connection, and ringing for history is worse than missing one fire.
 */
export function scheduledNotices(
	before: ReadonlyMap<string, string>,
	jobs: readonly JobEntry[],
): OsNotice[] {
	const notices: OsNotice[] = [];
	for (const job of jobs) {
		if (job.deliver_to !== "window" || !job.last_run) continue;
		const seen = before.get(job.id);
		if (seen === undefined || seen === job.last_run) continue;
		const failed = job.last_status === "failed";
		notices.push({
			title: failed ? "Scheduled job failed" : "Scheduled job ran",
			body: banner(job.last_summary ?? job.instruction),
			kind: "scheduled",
		});
	}
	return notices;
}

/** OS notifications fire only when the window is not the front one.
 *  A hidden window is unfocused, so approval and turn-complete still
 *  notify while the window is gone (TD-2902). */
export function shouldNotify(focused: boolean): boolean {
	return !focused;
}

/** Map a notification action onto the existing approve/deny verbs. */
export function noticeActionMessage(
	action: "approve" | "deny",
	sessionId: string,
	toolCallId: string,
): { type: "approve" | "deny"; session_id: string; tool_call_id: string } {
	return { type: action, session_id: sessionId, tool_call_id: toolCallId };
}
