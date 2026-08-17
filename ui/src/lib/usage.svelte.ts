// Usage and cost store (TD-1706).
//
// The panel is a right-pane tab, so the tab itself lives in
// right-pane.svelte.ts (TD-1707) and this store owns only the data: the
// rows the daemon sent, which bucket is selected, and the last export.
//
// Wiring mirrors the doctor store (TD-1104): listens on the connection
// fan-out, sends only via sendToDaemon — no client reference, no import
// cycle.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { showRightPane } from "./right-pane.svelte.js";
import type { DaemonEventUnion, UsageRollup } from "./protocol";
import type { UsageBucket } from "./usage";

/** What the last export produced, for the confirmation line. */
export interface ExportResult {
	format: "jsonl" | "csv";
	path: string;
	rows: number;
}

export const usage = $state({
	/** True between asking and the report landing. */
	loading: false,
	/** True once a report has arrived, so "no spend yet" and "not asked
	 *  yet" render differently — an empty table is a real answer. */
	loaded: false,
	rows: [] as UsageRollup[],
	bucket: "day" as UsageBucket,
	exporting: false,
	lastExport: null as ExportResult | null,
	error: null as string | null,
});

let started = false;

/** Register the reducer once. Returns the unsubscribe for tests. */
export function startUsage(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetUsage(): void {
	usage.loading = false;
	usage.loaded = false;
	usage.rows = [];
	usage.bucket = "day";
	usage.exporting = false;
	usage.lastExport = null;
	usage.error = null;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "usage_report") {
		usage.loading = false;
		usage.loaded = true;
		usage.rows = event.rows;
		usage.error = null;
		return;
	}
	if (event.type === "usage_exported") {
		usage.exporting = false;
		usage.lastExport = { format: event.format, path: event.path, rows: event.rows };
		usage.error = null;
		return;
	}
	// An export that failed on the daemon side comes back as a typed error,
	// not a usage_exported with a path that isn't there. Clear the in-flight
	// flag or the buttons stay dead until a reload.
	if (event.type === "error" && usage.exporting) {
		usage.exporting = false;
		usage.error = event.message;
	}
}

/** Ask the daemon for the rollups. A request while one is in flight is
 *  ignored — the answer covers every bucket, so a second adds nothing. */
export function refreshUsage(): void {
	if (usage.loading) return;
	usage.loading = true;
	usage.error = null;
	if (!sendToDaemon({ type: "get_usage" })) {
		usage.loading = false;
		usage.error = "No connection to the daemon.";
	}
}

/** Show the usage tab and load it. This is what the title-bar meter's
 *  hover panel links to. */
export function openUsage(): void {
	showRightPane("usage");
	refreshUsage();
}

export function selectBucket(bucket: UsageBucket): void {
	usage.bucket = bucket;
}

/** Export the model-call records, reusing the daemon's TD-903 exporters. */
export function exportUsage(format: "jsonl" | "csv"): void {
	if (usage.exporting) return;
	usage.exporting = true;
	usage.error = null;
	usage.lastExport = null;
	if (!sendToDaemon({ type: "export_usage", format })) {
		usage.exporting = false;
		usage.error = "No connection to the daemon.";
	}
}
