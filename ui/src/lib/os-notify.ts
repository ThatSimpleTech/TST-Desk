// OS notification copy (TD-1702).
//
// Pure: given a daemon event, either there is something the OS should say
// or there is not. Focus and permission live in the store — this file
// does not know about Tauri.

import type { DaemonEventUnion } from "./protocol";

export interface OsNotice {
	title: string;
	body: string;
	kind: "approval" | "turn";
}

/** What the OS should say for this event, or null if it is not a notify. */
export function noticeFor(event: DaemonEventUnion): OsNotice | null {
	if (event.type === "approval_request") {
		return {
			title: "Approval needed",
			body: event.summary || `The agent wants to run ${event.tool_name}`,
			kind: "approval",
		};
	}
	if (event.type === "turn_complete") {
		return {
			title: event.failed ? "Turn failed" : "Turn complete",
			body: event.failed
				? (event.error_code ?? "The turn did not finish")
				: "The agent finished a turn",
			kind: "turn",
		};
	}
	return null;
}

/** OS notifications fire only when the window is not the front one.
 *  A hidden window is unfocused, so approval and turn-complete still
 *  notify while the window is gone (TD-2902). */
export function shouldNotify(focused: boolean): boolean {
	return !focused;
}
