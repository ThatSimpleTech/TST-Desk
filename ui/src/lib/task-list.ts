// The native loop's task list (TD-5122): pure helpers the Plan pane and
// the chat's task strip share. The daemon sends the whole list each time;
// nothing here infers an item it was not given.

import type { TaskListItem, TaskStatus } from "./protocol";

const STATUS_LABELS: Record<TaskStatus, string> = {
	pending: "Pending",
	in_progress: "In progress",
	done: "Done",
};

export function statusLabel(status: TaskStatus): string {
	return STATUS_LABELS[status] ?? status;
}

/** "2 of 5 done", or "All 3 done". */
export function progressLabel(items: readonly TaskListItem[]): string {
	const done = items.filter((item) => item.status === "done").length;
	return done === items.length ? `All ${done} done` : `${done} of ${items.length} done`;
}

/** The item the strip names: the one in progress, else the next pending. */
export function currentTask(items: readonly TaskListItem[]): TaskListItem | null {
	return (
		items.find((item) => item.status === "in_progress") ??
		items.find((item) => item.status === "pending") ??
		null
	);
}

/** The chat strip shows while something is left to do. A finished list
 *  stays in the Plan pane only, so it does not sit above the composer. */
export function showTaskStrip(items: readonly TaskListItem[]): boolean {
	return items.some((item) => item.status !== "done");
}
