// TD-5240: whether each model on the picker can run right now, as the
// daemon last said. The picker asks when it opens (`check_presets`) and
// shows a model's problem on its row, before the click: no key stored, its
// local server not answering, the Grok CLI not found. A row the daemon has
// said nothing about shows nothing; the window does not guess.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, PresetReady } from "./protocol";

export const readiness = $state<{ rows: Record<string, PresetReady> }>({ rows: {} });

/** Keep the daemon's latest answer. Any other event is ignored. */
export function ingestReadiness(event: DaemonEventUnion): void {
	if (event.type !== "preset_readiness") return;
	readiness.rows = Object.fromEntries(event.rows.map((row) => [row.preset, row]));
}

/** What is wrong with `preset`, in one sentence, or "" when nothing is known to be. */
export function problemWith(preset: string): string {
	const row = readiness.rows[preset];
	return row === undefined || row.status === "ready" ? "" : (row.detail ?? "");
}

export function resetReadiness(): void {
	readiness.rows = {};
}

/** Listen for the daemon's answers while the picker is mounted. */
export function watchReadiness(): () => void {
	return onEvent(ingestReadiness);
}

/** Ask the daemon again. The picker calls this each time it opens. */
export function askReadiness(): void {
	sendToDaemon({ type: "check_presets" });
}
