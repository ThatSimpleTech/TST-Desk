// OS notifications when the window is unfocused (TD-1702).
//
// Permission is requested on the first qualifying event, never at startup.
// The bridge is injected so tests drive the same path the shell uses, and
// a browser / vitest run is a silent no-op.

import { onEvent } from "./connection-status.svelte.js";
import { noticeFor, shouldNotify } from "./os-notify";
import type { DaemonEventUnion } from "./protocol";

export interface OsNotifyBridge {
	isPermissionGranted(): Promise<boolean>;
	requestPermission(): Promise<string>;
	send(notice: { title: string; body: string }): Promise<void>;
}

let focused = true;
let decided = false;
let granted = false;
let started = false;
let bridge: OsNotifyBridge | null = null;

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
}

export function startOsNotify(injected?: OsNotifyBridge): () => void {
	if (started) return () => {};
	started = true;
	bridge = injected ?? null;
	const offFocus = listenFocus();
	const off = onEvent((event) => {
		void handleOsEvent(event);
	});
	if (injected === undefined) void listenNotificationClicks();
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
			const { sendNotification } = await import("@tauri-apps/plugin-notification");
			sendNotification({ title: notice.title, body: notice.body });
		},
	};
}

export async function handleOsEvent(event: DaemonEventUnion): Promise<void> {
	const notice = noticeFor(event);
	if (notice === null || !shouldNotify(focused) || bridge === null) return;
	if (!(await ensurePermission())) return;
	await bridge.send({ title: notice.title, body: notice.body });
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
