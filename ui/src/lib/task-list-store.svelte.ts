// Task lists per session (TD-5122), from the daemon's `task_list` events,
// and the plan the person approved to end plan mode (TD-5123), from its
// `plan_pinned` events.
//
// Keyed by session so switching chats shows each one's own list, and a
// bridged session's list (another computer's, forwarded by the hub) shows
// like a local one: it is only display. The newest event is the list, and
// the newest `plan_pinned` is the pinned plan.

import { onEvent } from "./connection-status.svelte.js";
import type { DaemonEventUnion, TaskListItem } from "./protocol";
import { showRightPane } from "./right-pane.svelte.js";
import { session } from "./session-status.svelte.js";

export const taskLists = $state({ bySession: {} as Record<string, TaskListItem[]> });
export const pinnedPlans = $state({ bySession: {} as Record<string, string> });

const NONE: readonly TaskListItem[] = [];

export function taskListFor(sessionId: string | null): readonly TaskListItem[] {
	if (sessionId === null) return NONE;
	return taskLists.bySession[sessionId] ?? NONE;
}

/** The plan the person approved for this session, or "" (TD-5123). */
export function pinnedPlanFor(sessionId: string | null): string {
	if (sessionId === null) return "";
	return pinnedPlans.bySession[sessionId] ?? "";
}

let started = false;

export function startTaskLists(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetTaskLists(): void {
	started = false;
	taskLists.bySession = {};
	pinnedPlans.bySession = {};
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "plan_pinned") {
		pinnedPlans.bySession[event.session_id] = event.plan;
		return;
	}
	if (event.type !== "task_list") return;
	const hadList = (taskLists.bySession[event.session_id] ?? NONE).length > 0;
	const items = event.items ?? [];
	taskLists.bySession[event.session_id] = items;
	// A new list opens the Plan pane, as Grok's plan does. Later updates
	// leave the pane where the user put it.
	if (!hadList && items.length > 0 && event.session_id === session.sessionId) {
		showRightPane("plan");
	}
}
