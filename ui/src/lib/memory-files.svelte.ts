// Memory-column store (TD-2601 / TD-2602).
//
// Lists a workspace's `.tst/memory/*.md` from the daemon (not a disk
// walk in the UI). Save is a human-path client message, never a tool.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, MemoryFileEntry } from "./protocol";

export const memoryFiles = $state({
	workspacePath: null as string | null,
	files: [] as MemoryFileEntry[],
	selectedPath: null as string | null,
	editing: false,
	draft: "",
	saving: false,
	error: null as string | null,
});

let started = false;

export function startMemoryFiles(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetMemoryFiles(): void {
	started = false;
	memoryFiles.workspacePath = null;
	memoryFiles.files = [];
	memoryFiles.selectedPath = null;
	memoryFiles.editing = false;
	memoryFiles.draft = "";
	memoryFiles.saving = false;
	memoryFiles.error = null;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "memory_files") {
		if (memoryFiles.workspacePath !== event.workspace_path) return;
		memoryFiles.files = event.files;
		memoryFiles.saving = false;
		memoryFiles.error = null;
		if (
			memoryFiles.selectedPath !== null &&
			!event.files.some((file) => file.path === memoryFiles.selectedPath)
		) {
			memoryFiles.selectedPath = null;
			memoryFiles.editing = false;
			memoryFiles.draft = "";
		} else if (memoryFiles.editing) {
			memoryFiles.editing = false;
			memoryFiles.draft = "";
		}
		return;
	}
	if (
		event.type === "error" &&
		(event.code === "not_a_memory_file" || event.code === "memory_cap")
	) {
		memoryFiles.error = event.message;
		memoryFiles.saving = false;
	}
}

/** Bind the column to a workspace and ask the daemon for its files. */
export function loadMemoryFiles(workspacePath: string): void {
	memoryFiles.workspacePath = workspacePath;
	memoryFiles.files = [];
	memoryFiles.selectedPath = null;
	memoryFiles.editing = false;
	memoryFiles.draft = "";
	memoryFiles.saving = false;
	memoryFiles.error = null;
	sendToDaemon({ type: "list_memory", workspace_path: workspacePath });
}

export function selectMemoryFile(path: string): void {
	memoryFiles.selectedPath = path;
	memoryFiles.editing = false;
	memoryFiles.draft = "";
	memoryFiles.error = null;
}

export function selectedMemoryFile(): MemoryFileEntry | null {
	if (memoryFiles.selectedPath === null) return null;
	return memoryFiles.files.find((file) => file.path === memoryFiles.selectedPath) ?? null;
}

export function beginEdit(): void {
	const file = selectedMemoryFile();
	if (file === null) return;
	memoryFiles.editing = true;
	memoryFiles.draft = file.content;
	memoryFiles.error = null;
}

export function cancelEdit(): void {
	memoryFiles.editing = false;
	memoryFiles.draft = "";
	memoryFiles.error = null;
}

export function setMemoryDraft(value: string): void {
	memoryFiles.draft = value;
}

export function saveMemoryFile(): boolean {
	const workspacePath = memoryFiles.workspacePath;
	const file = selectedMemoryFile();
	if (workspacePath === null || file === null) return false;
	if (
		!sendToDaemon({
			type: "save_memory",
			workspace_path: workspacePath,
			path: file.name,
			content: memoryFiles.draft,
		})
	) {
		return false;
	}
	memoryFiles.saving = true;
	memoryFiles.error = null;
	return true;
}
