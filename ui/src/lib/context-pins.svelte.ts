// Context-column store (TD-2804).
//
// Pins come from the daemon. Search filters locally. + is a picker;
// the wall check is the daemon's.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { ContextPinEntry, DaemonEventUnion } from "./protocol";

export const contextPins = $state({
	workspacePath: null as string | null,
	pins: [] as ContextPinEntry[],
	query: "",
	error: null as string | null,
});

let started = false;

export function startContextPins(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetContextPins(): void {
	started = false;
	contextPins.workspacePath = null;
	contextPins.pins = [];
	contextPins.query = "";
	contextPins.error = null;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "context_pins") {
		if (contextPins.workspacePath !== event.workspace_path) return;
		contextPins.pins = event.pins;
		contextPins.error = null;
		return;
	}
	if (event.type === "error" && event.code === "outside_workspace") {
		contextPins.error = event.message;
	}
}

export function loadContextPins(workspacePath: string): void {
	contextPins.workspacePath = workspacePath;
	contextPins.pins = [];
	contextPins.query = "";
	contextPins.error = null;
	sendToDaemon({ type: "list_pins", workspace_path: workspacePath });
}

export function addContextPin(path: string): boolean {
	const workspace = contextPins.workspacePath;
	if (workspace === null || path.trim() === "") return false;
	return sendToDaemon({ type: "add_pin", workspace_path: workspace, path });
}

export function removeContextPin(path: string): boolean {
	const workspace = contextPins.workspacePath;
	if (workspace === null) return false;
	return sendToDaemon({ type: "remove_pin", workspace_path: workspace, path });
}

export function setPinQuery(query: string): void {
	contextPins.query = query;
}
