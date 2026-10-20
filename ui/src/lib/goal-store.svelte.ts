// Session goals (TD-5124), from the daemon's `session_goal` events.
//
// Keyed by session so each chat shows its own. Display only: `/goal` is
// typed in the composer and the daemon sets, checks and clears the goal.
// The newest event is the goal; null once it is cleared or met.

import { onEvent } from "./connection-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";

export const goals = $state({ bySession: {} as Record<string, string | null> });

export function goalFor(sessionId: string | null): string | null {
	if (sessionId === null) return null;
	return goals.bySession[sessionId] ?? null;
}

let started = false;

export function startGoals(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetGoals(): void {
	started = false;
	goals.bySession = {};
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "session_goal") return;
	goals.bySession[event.session_id] = event.goal ?? null;
}
