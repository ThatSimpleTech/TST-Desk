// Grok engine UI state: slash commands, plan, mode, preview, TUI sessions.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { showRightPane } from "./right-pane.svelte.js";
import type {
	DaemonEventUnion,
	GrokCommand,
	GrokExtension,
	GrokPlanEntry,
	GrokPreview,
	GrokSessionEntry,
} from "./protocol";

export const grok = $state({
	commands: [] as GrokCommand[],
	planMarkdown: "",
	planEntries: [] as GrokPlanEntry[],
	mode: "",
	modes: [] as string[],
	preview: null as GrokPreview | null,
	sessions: [] as GrokSessionEntry[],
	extensions: [] as GrokExtension[],
});

let started = false;

export function startGrok(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	sendToDaemon({ type: "list_grok_sessions" });
	sendToDaemon({ type: "list_grok_extensions" });
	return () => {
		started = false;
		off();
	};
}

export function resetGrok(): void {
	started = false;
	grok.commands = [];
	grok.planMarkdown = "";
	grok.planEntries = [];
	grok.mode = "";
	grok.modes = [];
	grok.preview = null;
	grok.sessions = [];
	grok.extensions = [];
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "grok_commands") {
		grok.commands = event.commands;
		return;
	}
	if (event.type === "grok_plan") {
		grok.planMarkdown = event.markdown ?? "";
		grok.planEntries = event.entries ?? [];
		showRightPane("plan");
		return;
	}
	if (event.type === "grok_mode") {
		grok.mode = event.mode;
		grok.modes = event.modes ?? [];
		return;
	}
	if (event.type === "grok_preview") {
		grok.preview = event;
		showRightPane("preview");
		return;
	}
	if (event.type === "grok_session_list") {
		grok.sessions = event.sessions;
		return;
	}
	if (event.type === "grok_extensions") {
		grok.extensions = event.items;
	}
}

export function setGrokMode(sessionId: string, mode: string): void {
	sendToDaemon({ type: "set_grok_mode", session_id: sessionId, mode });
}

export function runGrokCommand(sessionId: string, name: string, argument = ""): void {
	sendToDaemon({ type: "run_grok_command", session_id: sessionId, name, argument });
}

export function approveGrokPlan(sessionId: string, comment = ""): void {
	sendToDaemon({ type: "approve_grok_plan", session_id: sessionId, comment });
}

export function openInTerminal(sessionId: string): void {
	sendToDaemon({ type: "open_in_terminal", session_id: sessionId });
}

export function matchingGrokCommands(query: string): GrokCommand[] {
	const q = query.trim().replace(/^\//, "").toLowerCase();
	if (q === "") return grok.commands.slice(0, 12);
	return grok.commands.filter(
		(c) =>
			c.name.toLowerCase().startsWith(q) ||
			(c.description ?? "").toLowerCase().includes(q),
	).slice(0, 12);
}
