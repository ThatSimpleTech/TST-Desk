// Memory-column store (TD-2601).
//
// Lists a workspace's `.tst/memory/*.md` from the daemon (not a disk
// walk in the UI). Opening a file shows its markdown; the editor path
// is TD-2602.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, MemoryFileEntry } from "./protocol";

export const memoryFiles = $state({
	workspacePath: null as string | null,
	files: [] as MemoryFileEntry[],
	selectedPath: null as string | null,
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
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "memory_files") return;
	if (memoryFiles.workspacePath !== event.workspace_path) return;
	memoryFiles.files = event.files;
	if (
		memoryFiles.selectedPath !== null &&
		!event.files.some((file) => file.path === memoryFiles.selectedPath)
	) {
		memoryFiles.selectedPath = null;
	}
}

/** Bind the column to a workspace and ask the daemon for its files. */
export function loadMemoryFiles(workspacePath: string): void {
	memoryFiles.workspacePath = workspacePath;
	memoryFiles.files = [];
	memoryFiles.selectedPath = null;
	sendToDaemon({ type: "list_memory", workspace_path: workspacePath });
}

export function selectMemoryFile(path: string): void {
	memoryFiles.selectedPath = path;
}

export function selectedMemoryFile(): MemoryFileEntry | null {
	if (memoryFiles.selectedPath === null) return null;
	return memoryFiles.files.find((file) => file.path === memoryFiles.selectedPath) ?? null;
}
