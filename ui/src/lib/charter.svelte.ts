// Charter-column store (TD-4002).
//
// Loads and saves `.tst/autonomy/CHARTER.md` through human-path client
// messages, never a tool.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { charterFromEvent, charterPayload, emptyCharterDraft } from "./charter";
import type { CharterFields, DaemonEventUnion } from "./protocol";

export const charter = $state({
	workspacePath: null as string | null,
	present: false,
	draft: emptyCharterDraft(),
	notes: "",
	saving: false,
	error: null as string | null,
});

let started = false;

export function startCharter(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetCharter(): void {
	started = false;
	charter.workspacePath = null;
	charter.present = false;
	charter.draft = emptyCharterDraft();
	charter.notes = "";
	charter.saving = false;
	charter.error = null;
}

function applyDocument(
	present: boolean,
	fields: CharterFields | null | undefined,
	notes: string | undefined,
): void {
	charter.present = present;
	charter.draft = fields ? charterFromEvent(fields) : emptyCharterDraft();
	charter.notes = notes ?? "";
	charter.saving = false;
	charter.error = null;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "charter") {
		if (charter.workspacePath !== event.workspace_path) return;
		applyDocument(event.present, event.charter, event.notes);
		return;
	}
	if (event.type === "error" && event.code === "invalid_charter") {
		charter.error = event.message;
		charter.saving = false;
	}
}

/** Bind the column to a workspace and ask the daemon for its charter. */
export function loadCharter(workspacePath: string): void {
	charter.workspacePath = workspacePath;
	charter.present = false;
	charter.draft = emptyCharterDraft();
	charter.notes = "";
	charter.saving = false;
	charter.error = null;
	sendToDaemon({ type: "get_charter", workspace_path: workspacePath });
}

export function setObjective(value: string): void {
	charter.draft.objective = value;
}

export function setNotes(value: string): void {
	charter.notes = value;
}

export function setListItem(
	field: "definition_of_done" | "source_of_truth" | "stop_conditions",
	index: number,
	value: string,
): void {
	charter.draft[field][index] = value;
}

export function addListItem(
	field: "definition_of_done" | "source_of_truth" | "stop_conditions",
): void {
	charter.draft[field] = [...charter.draft[field], ""];
}

export function removeListItem(
	field: "definition_of_done" | "source_of_truth" | "stop_conditions",
	index: number,
): void {
	const next = charter.draft[field].filter((_, i) => i !== index);
	charter.draft[field] = next.length === 0 ? [""] : next;
}

export function setBoundaryListItem(
	field: "writable_paths" | "allowed_commands",
	index: number,
	value: string,
): void {
	charter.draft.boundary[field][index] = value;
}

export function addBoundaryListItem(field: "writable_paths" | "allowed_commands"): void {
	charter.draft.boundary[field] = [...charter.draft.boundary[field], ""];
}

export function removeBoundaryListItem(
	field: "writable_paths" | "allowed_commands",
	index: number,
): void {
	const next = charter.draft.boundary[field].filter((_, i) => i !== index);
	charter.draft.boundary[field] = next.length === 0 ? [""] : next;
}

export function setNetworkDeny(): void {
	charter.draft.boundary.network = "deny";
}

export function setNetworkHosts(hosts: string[]): void {
	charter.draft.boundary.network = hosts.length === 0 ? [""] : hosts;
}

export function setHostItem(index: number, value: string): void {
	const current = charter.draft.boundary.network;
	const hosts = current === "deny" ? [""] : [...current];
	hosts[index] = value;
	charter.draft.boundary.network = hosts;
}

export function addHostItem(): void {
	const current = charter.draft.boundary.network;
	const hosts = current === "deny" ? [""] : [...current, ""];
	charter.draft.boundary.network = hosts;
}

export function removeHostItem(index: number): void {
	const current = charter.draft.boundary.network;
	if (current === "deny") return;
	const next = current.filter((_, i) => i !== index);
	charter.draft.boundary.network = next.length === 0 ? [""] : next;
}

export function setCap(
	field: "spend_usd" | "wall_clock_hours" | "max_iterations",
	value: number,
): void {
	if (!Number.isFinite(value)) return;
	charter.draft.caps[field] = value;
}

export function saveCharter(): boolean {
	const workspacePath = charter.workspacePath;
	if (workspacePath === null) return false;
	if (
		!sendToDaemon({
			type: "save_charter",
			workspace_path: workspacePath,
			charter: charterPayload(charter.draft),
			notes: charter.notes,
		})
	) {
		return false;
	}
	charter.saving = true;
	charter.error = null;
	return true;
}
