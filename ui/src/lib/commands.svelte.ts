// Slash-command store (TD-4501).
//
// Lists workspace commands from the daemon. The UI never invents names.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { CommandEntry, DaemonEventUnion } from "./protocol";

export const commands = $state({
	workspacePath: null as string | null,
	items: [] as CommandEntry[],
});

let started = false;

export function startCommands(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetCommands(): void {
	started = false;
	commands.workspacePath = null;
	commands.items = [];
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "command_list") return;
	if (commands.workspacePath !== event.workspace_path) return;
	commands.items = event.commands;
}

/** Bind to a workspace and ask the daemon for its slash commands. */
export function loadCommands(workspacePath: string): void {
	commands.workspacePath = workspacePath;
	commands.items = [];
	sendToDaemon({ type: "list_commands", workspace_path: workspacePath });
}
