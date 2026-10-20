// Task lists per session (TD-5122), from the daemon's `task_list` events.
//
// Keyed by session so switching chats shows each one's own list, and a
// bridged session's list (another computer's, forwarded by the hub) shows
// like a local one: it is only display. The newest event is the list.

import { onEvent } from "./connection-status.svelte.js";
import type { DaemonEventUnion, TaskListItem } from "./protocol";
import { showRightPane } from "./right-pane.svelte.js";
import { session } from "./session-status.svelte.js";

export const taskLists = $state({ bySession: {} as Record<string, TaskListItem[]> });

const NONE: readonly TaskListItem[] = [];

export function taskListFor(sessionId: string | null): readonly TaskListItem[] {
	if (sessionId === null) return NONE;
	return taskLists.bySession[sessionId] ?? NONE;
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
}

function reduce(event: DaemonEventUnion): void {
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
