// First-run copy for close ≠ quit (TD-2903).
//
// The host stamps `{user_data_dir}/close-is-not-quit.yaml` and emits
// once after the first hide. This module turns that payload into the
// same banner the rest of the chrome uses. Do not nag.

import { notify } from "./notifications.svelte.js";

export const CLOSE_IS_NOT_QUIT_EVENT = "close-is-not-quit";

export const CLOSE_IS_NOT_QUIT = {
	title: "TST Desk is still running",
	body: "Closing the window hides TST Desk; Quit TST Desk is what stops it.",
} as const;

export function showCloseIsNotQuitNotice(payload: {
	title?: string;
	body?: string;
} = {}): void {
	notify("close-is-not-quit", {
		severity: "banner",
		title: payload.title ?? CLOSE_IS_NOT_QUIT.title,
		body: payload.body ?? CLOSE_IS_NOT_QUIT.body,
	});
}

/** Listen for the host's one-shot event. No-op outside Tauri. */
export async function startCloseHint(): Promise<() => void> {
	try {
		const { listen } = await import("@tauri-apps/api/event");
		return listen<{ title?: string; body?: string }>(CLOSE_IS_NOT_QUIT_EVENT, (ev) => {
			showCloseIsNotQuitNotice(ev.payload);
		});
	} catch {
		return () => {};
	}
}
