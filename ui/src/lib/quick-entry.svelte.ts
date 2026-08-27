// Quick-entry overlay store (TD-4702).
//
// Separate from the main chat pane: connects, binds to the last workspace,
// sends one message, then hides. Does not mount AppShell or initChat.

import {
	attachToSession,
	connect,
	onDaemonEvent,
	sendToDaemon,
	ws,
} from "./connection-status.svelte.js";
import { hideQuickEntryOverlay, readLastWorkspace } from "./last-workspace";
import { pickQuickEntrySession } from "./quick-entry";
import type { DaemonEventUnion } from "./protocol";

export const quickEntry = $state({
	phase: "connecting" as "connecting" | "ready" | "error",
	workspacePath: null as string | null,
	sessionId: null as string | null,
	error: null as string | null,
	draft: "",
});

let targetPath: string | null = null;
let waitingOpen = false;
let started = false;

function resetQuickEntry(): void {
	quickEntry.phase = "connecting";
	quickEntry.workspacePath = null;
	quickEntry.sessionId = null;
	quickEntry.error = null;
	quickEntry.draft = "";
	targetPath = null;
	waitingOpen = false;
}

function reduce(event: DaemonEventUnion): void {
	if (quickEntry.phase === "error") return;

	if (event.type === "session_list") {
		const pick = pickQuickEntrySession(event.sessions, targetPath);
		if (pick.action === "none") {
			quickEntry.phase = "error";
			quickEntry.error = "Open a workspace in TST Desk first.";
			return;
		}
		if (pick.action === "attach") {
			quickEntry.sessionId = pick.sessionId;
			attachToSession(pick.sessionId);
			quickEntry.phase = "ready";
			return;
		}
		if (pick.action === "open" && targetPath !== null) {
			waitingOpen = true;
			sendToDaemon({ type: "open_workspace", path: targetPath });
		}
		return;
	}

	if (event.type === "session_state" && waitingOpen) {
		quickEntry.sessionId = event.session_id;
		attachToSession(event.session_id);
		waitingOpen = false;
		quickEntry.phase = "ready";
	}
}

/** Connect and bind to the last workspace. Idempotent; returns unsubscribe. */
export async function bootQuickEntry(): Promise<() => void> {
	if (started) return () => {};
	started = true;
	resetQuickEntry();

	await connect();
	targetPath = await readLastWorkspace();
	if (targetPath === null) {
		quickEntry.phase = "error";
		quickEntry.error = "Open a workspace in TST Desk first.";
		return () => {
			started = false;
		};
	}
	quickEntry.workspacePath = targetPath;

	const offEvents = onDaemonEvent(reduce);
	sendToDaemon({ type: "list_sessions" });

	return () => {
		started = false;
		offEvents();
	};
}

/** Send the draft to the bound session and hide the overlay. */
export function sendQuickEntry(): boolean {
	const text = quickEntry.draft.trim();
	if (quickEntry.sessionId === null || text === "" || quickEntry.phase !== "ready") {
		return false;
	}
	if (ws.state !== "connected") return false;
	const sent = sendToDaemon({
		type: "user_message",
		session_id: quickEntry.sessionId,
		content: text,
	});
	if (!sent) return false;
	quickEntry.draft = "";
	void hideQuickEntryOverlay();
	return true;
}

/** Hide without sending (Esc / close). */
export function dismissQuickEntry(): void {
	void hideQuickEntryOverlay();
}

/** Reset for tests. */
export function resetQuickEntryStore(): void {
	started = false;
	resetQuickEntry();
}
