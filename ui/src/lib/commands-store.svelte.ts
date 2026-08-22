// Slash-command store (TD-4501).
//
// Holds the workspace's command listing and the composer menu's state. The
// listing comes from the daemon's `commands_list` — nothing is inferred
// locally (AGENTS §6) — and the menu's visibility is derived from the draft
// in Composer.svelte, mirrored here so the shell's Escape layer (shortcuts.ts)
// sees one open flag instead of each component's opinion.
//
// Wiring mirrors settings.svelte.ts: listens on the connection fan-out, sends
// only via sendToDaemon — no client reference, no import cycle.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { nextIndex } from "./palette";
import type { CommandSummary, DaemonEventUnion } from "./protocol";

export const commandMenu = $state({
	/** The composer's menu is showing (draft is a slash query, not dismissed). */
	open: false,
	/** The live slash query, synced by Composer so Escape can dismiss it. */
	query: "",
	/** The listing, from commands_list. Empty until the first reply lands. */
	commands: [] as CommandSummary[],
	/** Session the listing was fetched for; null = never. */
	fetchedFor: null as string | null,
	/** Session whose request is in flight (the reply carries no echo, so the
	 *  request side is what marks it). */
	pendingFor: null as string | null,
	/** The query Escape dismissed; typing past it reopens the menu. */
	dismissedQuery: null as string | null,
	/** Highlighted row in the menu. */
	selected: 0,
});

let started = false;

/** Register the reducer. Unsubscribe for tests. */
export function startCommandMenu(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetCommandMenu(): void {
	commandMenu.open = false;
	commandMenu.query = "";
	commandMenu.commands = [];
	commandMenu.fetchedFor = null;
	commandMenu.pendingFor = null;
	commandMenu.dismissedQuery = null;
	commandMenu.selected = 0;
	started = false;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "commands_list") return;
	commandMenu.commands = event.commands;
	commandMenu.fetchedFor = commandMenu.pendingFor;
	commandMenu.pendingFor = null;
	commandMenu.selected = 0;
}

/** Composer keeps the store's open/query mirror current — one writer, so
 *  the shell reads state instead of guessing from drafts it can't see. */
export function setSlashState(open: boolean, query: string): void {
	commandMenu.open = open;
	commandMenu.query = query;
}

/** Escape (via shortcuts.ts): close until the query changes. */
export function dismissSlashMenu(): void {
	if (!commandMenu.open) return;
	commandMenu.dismissedQuery = commandMenu.query;
	commandMenu.selected = 0;
}

/** Fetch the listing for *sessionId* once; safe on every keystroke. */
export function ensureCommands(sessionId: string): void {
	if (commandMenu.fetchedFor === sessionId || commandMenu.pendingFor === sessionId) return;
	commandMenu.pendingFor = sessionId;
	sendToDaemon({ type: "list_commands", session_id: sessionId });
}

/** Where ↑/↓ lands; wraps both ways. */
export function moveSelection(delta: number, count: number): void {
	commandMenu.selected = nextIndex(commandMenu.selected, delta, count);
}

/** A new query or listing puts the highlight back on the first row. */
export function resetSelection(): void {
	commandMenu.selected = 0;
}
