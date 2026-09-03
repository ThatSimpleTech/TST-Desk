// OS notifications when the window is unfocused (TD-1702).
//
// Permission is requested on the first qualifying event, never at startup.
// The bridge is injected so tests drive the same path the shell uses, and
// a browser / vitest run is a silent no-op.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import {
	type OsNotice,
	noticeActionMessage,
	noticeFor,
	runStamps,
	scheduledNotices,
	shouldNotify,
} from "./os-notify";
import type { DaemonEventUnion, JobEntry } from "./protocol";

export interface OsNotifyBridge {
	isPermissionGranted(): Promise<boolean>;
	requestPermission(): Promise<string>;
	send(notice: {
		title: string;
		body: string;
		sessionId?: string;
		toolCallId?: string;
		actions?: ReadonlyArray<{ id: "approve" | "deny"; title: string }>;
	}): Promise<void>;
}

let focused = true;
let decided = false;
let granted = false;
let started = false;
let bridge: OsNotifyBridge | null = null;
// Last `last_run` seen per job. Null until the first `job_list`, which
// seeds it silently — otherwise connecting would ring once for every job
// that has ever fired.
let seenRuns: Map<string, string> | null = null;

export function isWindowFocused(): boolean {
	return focused;
}

/** Tests and the window-focus listeners both set this. Default is focused. */
export function setWindowFocused(value: boolean): void {
	focused = value;
}

export function resetOsNotify(): void {
	focused = true;
	decided = false;
	granted = false;
	started = false;
	bridge = null;
	seenRuns = null;
}

export function startOsNotify(injected?: OsNotifyBridge): () => void {
	if (started) return () => {};
	started = true;
	bridge = injected ?? null;
	const offFocus = listenFocus();
	const off = onEvent((event) => {
		void handleOsEvent(event);
	});
	if (injected === undefined) {
		void listenNotificationClicks();
		void listenApprovalActions();
	}
	return () => {
		started = false;
		off();
		offFocus();
	};
}

async function listenNotificationClicks(): Promise<void> {
	try {
		const mod = await import("@tauri-apps/plugin-notification");
		const onAction = (mod as { onAction?: (cb: () => void) => Promise<unknown> }).onAction;
		if (onAction === undefined) return;
		await onAction(() => {
			void import("@tauri-apps/api/window").then(({ getCurrentWindow }) =>
				getCurrentWindow().setFocus(),
			);
		});
	} catch {
		// Browser / tests / a plugin build without onAction.
	}
}

function listenFocus(): () => void {
	if (typeof window === "undefined") return () => {};
	const on = () => setWindowFocused(true);
	const off = () => setWindowFocused(false);
	window.addEventListener("focus", on);
	window.addEventListener("blur", off);
	return () => {
		window.removeEventListener("focus", on);
		window.removeEventListener("blur", off);
	};
}

/** The real OS bridge. Dynamic import so vitest never loads Tauri. */
export function createTauriOsNotifyBridge(): OsNotifyBridge {
	return {
		async isPermissionGranted() {
			const { isPermissionGranted } = await import("@tauri-apps/plugin-notification");
			return isPermissionGranted();
		},
		async requestPermission() {
			const { requestPermission } = await import("@tauri-apps/plugin-notification");
			return requestPermission();
		},
		async send(notice) {
			if (notice.actions && notice.sessionId && notice.toolCallId) {
				try {
					const { invoke } = await import("@tauri-apps/api/core");
					await invoke("approval_notice", {
						title: notice.title,
						body: notice.body,
						sessionId: notice.sessionId,
						toolCallId: notice.toolCallId,
					});
					return;
				} catch {
					// Fall through to a banner without buttons.
				}
			}
			const { sendNotification } = await import("@tauri-apps/plugin-notification");
			sendNotification({ title: notice.title, body: notice.body });
		},
	};
}

async function listenApprovalActions(): Promise<void> {
	try {
		const { listen } = await import("@tauri-apps/api/event");
		await listen<{
			session_id: string;
			tool_call_id: string;
			action: "approve" | "deny" | "show";
		}>("approval-action", (event) => {
			handleApprovalAction(event.payload);
		});
	} catch {
		// Browser / tests.
	}
}

export function handleApprovalAction(payload: {
	session_id: string;
	tool_call_id: string;
	action: "approve" | "deny" | "show";
}): void {
	if (payload.action === "show") return;
	sendToDaemon(noticeActionMessage(payload.action, payload.session_id, payload.tool_call_id));
}

export async function handleOsEvent(event: DaemonEventUnion): Promise<void> {
	// Tracking runs happens before every early return: drop a `job_list`
	// because the window was focused and the *next* one would look like a
	// fresh run and ring for something the user already watched happen.
	const notices = noticesFor(event);
	if (notices.length === 0 || !shouldNotify(focused) || bridge === null) return;
	if (!(await ensurePermission())) return;
	for (const notice of notices) {
		await bridge.send({
			title: notice.title,
			body: notice.body,
			sessionId: notice.sessionId,
			toolCallId: notice.toolCallId,
			actions: notice.actions,
		});
	}
}

/** Every notice this event is worth. A `job_list` may carry more than one. */
function noticesFor(event: DaemonEventUnion): OsNotice[] {
	if (event.type === "job_list") return trackScheduledRuns(event.jobs);
	const one = noticeFor(event);
	return one === null ? [] : [one];
}

function trackScheduledRuns(jobs: readonly JobEntry[]): OsNotice[] {
	const next = runStamps(jobs);
	if (seenRuns === null) {
		seenRuns = next;
		return [];
	}
	const notices = scheduledNotices(seenRuns, jobs);
	seenRuns = next;
	return notices;
}

async function ensurePermission(): Promise<boolean> {
	if (decided) return granted;
	decided = true;
	if (await bridge!.isPermissionGranted()) {
		granted = true;
		return true;
	}
	granted = (await bridge!.requestPermission()) === "granted";
	return granted;
}
