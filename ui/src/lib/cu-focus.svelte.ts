// Bring the window forward when a local computer-use episode ends (TD-4832).
//
// The daemon emits `focus_window` live when the last CU episode closes —
// never between tool calls, never from replay (the event is not logged).
// Here we only execute the window action: show, unminimize, focus. A
// browser (remote attach) or test run is a silent no-op, and a focus
// failure must never surface as a turn error — it is a courtesy.

import { onDaemonEvent } from "./connection-status.svelte.js";
import { isTauri } from "./open-file";

export interface CuFocusBridge {
	/** Show, unminimize, and focus the app window. */
	activate(): Promise<void>;
}

let started = false;

/** The real host bridge. Dynamic import so vitest never loads Tauri. */
export function createTauriCuFocusBridge(): CuFocusBridge {
	return {
		async activate() {
			const { getCurrentWindow } = await import("@tauri-apps/api/window");
			const win = getCurrentWindow();
			await win.unminimize();
			await win.show();
			await win.setFocus();
		},
	};
}

/** Subscribe to `focus_window`. Returns an unsubscribe fn for tests. */
export function startCuFocus(injected?: CuFocusBridge): () => void {
	if (started) return () => {};
	started = true;
	const bridge = injected ?? (isTauri() ? createTauriCuFocusBridge() : null);
	const off = onDaemonEvent((event) => {
		if (event.type !== "focus_window" || bridge === null) return;
		void bridge.activate().catch(() => {
			// A focus failure is invisible by design — never a turn error.
		});
	});
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetCuFocus(): void {
	started = false;
}
