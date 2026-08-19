// Instructions-column store (TD-2802).
//
// Lists a workspace's steering files from the daemon (not a disk walk in
// the UI). Create is a human-path client message, never fs_write.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, InstructionFileEntry } from "./protocol";
import { openInEditor } from "./open-file";

export const instructions = $state({
	workspacePath: null as string | null,
	files: [] as InstructionFileEntry[],
	draftName: "",
	naming: false,
	error: null as string | null,
});

let started = false;

export function startInstructions(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetInstructions(): void {
	started = false;
	instructions.workspacePath = null;
	instructions.files = [];
	instructions.draftName = "";
	instructions.naming = false;
	instructions.error = null;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "instruction_files") {
		if (instructions.workspacePath !== event.workspace_path) return;
		instructions.files = event.files;
		instructions.error = null;
		if (event.created) void openInEditor(event.created);
		return;
	}
	if (event.type === "error" && event.code === "invalid_rule_name") {
		instructions.error = event.message;
	}
}

/** Bind the column to a workspace and ask the daemon for its files. */
export function loadInstructions(workspacePath: string): void {
	instructions.workspacePath = workspacePath;
	instructions.files = [];
	instructions.draftName = "";
	instructions.naming = false;
	instructions.error = null;
	sendToDaemon({ type: "list_instructions", workspace_path: workspacePath });
}

export function beginNewRule(): void {
	instructions.naming = true;
	instructions.draftName = "";
	instructions.error = null;
}

export function cancelNewRule(): void {
	instructions.naming = false;
	instructions.draftName = "";
}

export function createRule(): boolean {
	const path = instructions.workspacePath;
	const name = instructions.draftName.trim();
	if (path === null || name === "") return false;
	if (!sendToDaemon({ type: "create_rule", workspace_path: path, name })) return false;
	instructions.naming = false;
	instructions.draftName = "";
	return true;
}

export function setDraftName(value: string): void {
	instructions.draftName = value;
}
