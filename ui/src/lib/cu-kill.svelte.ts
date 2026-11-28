// Computer-use kill-switch chrome (TD-3404).
//
// The latch is process-wide on the daemon. This store does not invent it:
// it starts not-killed (the daemon default) and only flips when
// `cu_kill_state` arrives. Title bar, palette, and the shortcut all send
// `set_cu_kill`; they do not keep a second copy of the flag.
//
// TD-5223: the latch is machine-wide now (the tst-cu-mcp stop-file). A
// `cu_kill_state` can arrive unasked, when another tool creates or removes
// the file, or on connect while it exists; this store takes it the same way.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { notify } from "./notifications.svelte.js";
import type { DaemonEventUnion } from "./protocol";

/** Sent by the host when the stop shortcut could not write the stop-file (CU-05). */
export const STOP_FALLBACK_EVENT = "cu-stop-fallback";

export const cuKill = $state({
	killed: false,
});

let started = false;

function reduce(event: DaemonEventUnion): void {
	if (event.type === "cu_kill_state") {
		cuKill.killed = event.killed;
		// CU-05: Stop held in TST Desk; the machine-wide file did not.
		if (event.killed && event.file_error) {
			notify("cu:stop-file", {
				severity: "banner",
				title: "Computer use is stopped here, but not everywhere",
				body: `TST Desk stopped its own computer use, but could not write the stop file (${event.file_error}), so other agents on this computer may still act. Check that your home folder can be written.`,
			});
		}
	}
}

/** Register the reducer once. Returns the unsubscribe for tests. */
export function startCuKill(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	let offHost: () => void = () => {};
	void listenStopFallback().then((stop) => {
		offHost = stop;
	});
	return () => {
		started = false;
		off();
		offHost();
	};
}

/** CU-05: the shortcut's stop-file write failed, so hold Stop in the daemon. */
export function stopAfterShortcutFailure(): void {
	setCuKill(true);
}

async function listenStopFallback(): Promise<() => void> {
	try {
		const { listen } = await import("@tauri-apps/api/event");
		return await listen<string>(STOP_FALLBACK_EVENT, () => stopAfterShortcutFailure());
	} catch {
		return () => {};
	}
}

/** The ⌘. / Ctrl+. shortcut: engage only. It never resumes, already stopped
 * or not, so a key an agent can press cannot switch actuation back on. */
export function engageFromShortcut(): void {
	setCuKill(true);
}

/** Ask the daemon to engage or clear the switch. */
export function setCuKill(killed: boolean): void {
	sendToDaemon({ type: "set_cu_kill", killed });
}

export function resetCuKill(): void {
	cuKill.killed = false;
	started = false;
}
