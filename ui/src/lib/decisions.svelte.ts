// Decisions-ledger store (TD-1202).
//
// The panel's data is already on the wire: `decision_logged` events carry
// class, what, why, and the attributed commit (TD-704). This store filters
// the stream to the active session and derives the revert command — the
// same `git revert <sha>` the ledger file records (core/tstd/autonomy/
// ledger.py). No new protocol.
//
// Wiring mirrors the doctor store: connection fan-out in, no client
// reference, no import cycle.

import { onEvent } from "./connection-status.svelte.js";
import { session } from "./session-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";

/** The per-workspace ledger, relative to the workspace root. Mirrors
 * core/tstd/autonomy/ledger.py — the daemon writes, the panel links out. */
export const LEDGER_RELATIVE_PATH = ".tst/autonomy/DECISIONS.md";

export interface DecisionRow {
	/** Stable identity: `${session_id}:${seq}`. */
	id: string;
	seq: number;
	decisionClass: "A" | "B" | "C";
	what: string;
	why: string;
	commit: string | null;
	/** `git revert <sha>` when the decision names a commit — never for
	 * Class B entries with no attribution (TD-704 AC 3). */
	undoCommand: string | null;
}

export const decisions = $state({
	open: false,
	/** null = every class; "A"|"B"|"C" filters (AC: filterable by class). */
	classFilter: null as "A" | "B" | "C" | null,
	rows: [] as DecisionRow[],
});

let started = false;

/** Register the reducer once. Returns the unsubscribe for tests. */
export function startDecisions(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetDecisions(): void {
	decisions.open = false;
	decisions.classFilter = null;
	decisions.rows = [];
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "decision_logged") return;
	// The panel's scope is the active session (AC: session decisions).
	if (session.sessionId === null || event.session_id !== session.sessionId) return;
	decisions.rows.push({
		id: `${event.session_id}:${event.seq}`,
		seq: event.seq,
		decisionClass: event.decision_class,
		what: event.what,
		why: event.why,
		commit: event.commit ?? null,
		undoCommand: event.commit ? `git revert ${event.commit}` : null,
	});
}

export function openDecisions(): void {
	decisions.open = true;
}

export function closeDecisions(): void {
	decisions.open = false;
}

export function setClassFilter(cls: "A" | "B" | "C" | null): void {
	decisions.classFilter = cls;
}

/** Rows after the class filter, oldest first. */
export function filteredDecisions(): DecisionRow[] {
	if (decisions.classFilter === null) return decisions.rows;
	return decisions.rows.filter((r) => r.decisionClass === decisions.classFilter);
}

/** Absolute ledger path for the active workspace, or null when none is open. */
export function ledgerPath(): string | null {
	if (session.workspacePath === null) return null;
	return `${session.workspacePath}/${LEDGER_RELATIVE_PATH}`;
}
