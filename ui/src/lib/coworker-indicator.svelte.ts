// Coworker indicator wiring (TD-2904).
//
// Watches session states and window visibility. When the window is hidden
// and a session is running or awaiting approval, the host is told to badge
// the dock / taskbar. Showing the window clears the badge and focuses a
// parked approval if there is one. No tray.

import { onEvent } from "./connection-status.svelte.js";
import { pending } from "./approval-store.svelte.js";
import { chat, selectSession } from "./chat-store.svelte.js";
import { isTauri } from "./open-file";
import { focusSession } from "./session-status.svelte.js";
import { selectRow, sessions } from "./sessions.svelte.js";
import {
	approvalFocusOnReveal,
	coworkerBadge,
	firstAwaitingSession,
	focusApprovalCard,
} from "./coworker-indicator";
import type { DaemonEventUnion } from "./protocol";

export const WINDOW_VISIBILITY_EVENT = "window-visibility";

export interface CoworkerIndicatorBridge {
	setBadge(label: string | null): Promise<void>;
	focusCard(toolCallId: string): boolean;
	switchSession(sessionId: string): void;
}

const sessionStates = new Map<string, string>();

let started = false;
let windowHidden = false;
let lastBadge: string | null | undefined = undefined;
let bridge: CoworkerIndicatorBridge | null = null;

export function resetCoworkerIndicator(): void {
	started = false;
	windowHidden = false;
	lastBadge = undefined;
	bridge = null;
	sessionStates.clear();
}

/** Tests and the visibility listeners both set this. Default is shown. */
export function setWindowHidden(hidden: boolean): void {
	const revealed = windowHidden && !hidden;
	windowHidden = hidden;
	syncBadge();
	if (revealed) revealApprovals();
}

export function startCoworkerIndicator(injected?: CoworkerIndicatorBridge): () => void {
	if (started) return () => {};
	started = true;
	bridge = injected ?? createTauriCoworkerBridge();
	seedStates();
	const offEvents = onEvent(ingest);
	const offVis = listenDocumentVisibility();
	let offHost = () => {};
	if (injected === undefined) {
		void listenHostVisibility().then((off) => {
			offHost = off;
		});
	}
	syncBadge();
	return () => {
		started = false;
		bridge = null;
		offEvents();
		offVis();
		offHost();
	};
}

function seedStates(): void {
	for (const row of sessions.rows) {
		sessionStates.set(row.sessionId, row.state);
	}
	if (chat.sessionId !== null && chat.turnState !== null) {
		sessionStates.set(chat.sessionId, chat.turnState);
	}
}

function ingest(event: DaemonEventUnion): void {
	if (event.type === "session_list") {
		sessionStates.clear();
		for (const row of event.sessions) {
			sessionStates.set(row.session_id, row.state);
		}
		syncBadge();
		return;
	}
	if (event.type === "session_state") {
		sessionStates.set(event.session_id, event.state);
		syncBadge();
	}
}

function syncBadge(): void {
	if (bridge === null) return;
	const next = coworkerBadge(windowHidden, [...sessionStates.values()]);
	if (next === lastBadge) return;
	lastBadge = next;
	void bridge.setBadge(next);
}

function revealApprovals(): void {
	if (bridge === null) return;
	const action = approvalFocusOnReveal({
		revealed: true,
		pendingToolCallIds: pending.map((p) => p.toolCallId),
		awaitingSessionId: firstAwaitingSession(
			[...sessionStates.entries()].map(([sessionId, state]) => ({
				sessionId,
				state,
			})),
		),
		boundSessionId: chat.sessionId,
	});
	if (action.sessionId !== null) bridge.switchSession(action.sessionId);
	if (action.toolCallId !== null) bridge.focusCard(action.toolCallId);
}

function listenDocumentVisibility(): () => void {
	if (typeof document === "undefined") return () => {};
	const sync = (): void => {
		setWindowHidden(document.visibilityState === "hidden");
	};
	document.addEventListener("visibilitychange", sync);
	sync();
	return () => document.removeEventListener("visibilitychange", sync);
}

async function listenHostVisibility(): Promise<() => void> {
	try {
		const { listen } = await import("@tauri-apps/api/event");
		return listen<boolean>(WINDOW_VISIBILITY_EVENT, (ev) => {
			setWindowHidden(!ev.payload);
		});
	} catch {
		return () => {};
	}
}

export function createTauriCoworkerBridge(): CoworkerIndicatorBridge {
	return {
		async setBadge(label) {
			if (!isTauri()) return;
			const { invoke } = await import("@tauri-apps/api/core");
			await invoke("set_coworker_indicator", { label });
		},
		focusCard(toolCallId) {
			return focusApprovalCard(toolCallId);
		},
		switchSession(sessionId) {
			const row = sessions.rows.find((r) => r.sessionId === sessionId);
			if (row !== undefined) {
				selectRow(sessionId);
				return;
			}
			selectSession(sessionId, "awaiting_approval");
			focusSession(sessionId, "awaiting_approval");
		},
	};
}
