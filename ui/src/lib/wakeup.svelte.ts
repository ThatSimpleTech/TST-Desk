// Wake-up summary store (TD-4303).
//
// The card's data is already on the wire: `autonomy_summary` carries
// reason, branch, ledger path, changed paths, refusals, and the excerpt.
// This store keeps the latest event for the bound session. The UI never
// invents a summary from other events.

import { onEvent } from "./connection-status.svelte.js";
import { session } from "./session-status.svelte.js";
import type { AutonomySummary, DaemonEventUnion } from "./protocol";

export const wakeup = $state({
	summary: null as AutonomySummary | null,
});

let started = false;
let boundSessionId: string | null = null;
let lastSeq = 0;

/** Register the reducer once. Returns the unsubscribe for tests. */
export function startWakeup(): () => void {
	if (started) return () => {};
	started = true;
	if (boundSessionId === null) bindWakeup(session.sessionId);
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetWakeup(): void {
	wakeup.summary = null;
	boundSessionId = null;
	lastSeq = 0;
	started = false;
}

/**
 * Show a different session's wake-up card.
 *
 * Same contract as the timeline: the daemon's event log is the record, so
 * a switch drops what the previous session left here and lets attach
 * replay rebuild it. Re-binding the session already shown is a no-op.
 */
export function bindWakeup(sessionId: string | null): void {
	if (sessionId === boundSessionId) return;
	boundSessionId = sessionId;
	wakeup.summary = null;
	lastSeq = 0;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "autonomy_summary") return;
	if (boundSessionId === null || event.session_id !== boundSessionId) return;
	if (event.seq <= lastSeq) return;
	lastSeq = event.seq;
	wakeup.summary = event;
}
