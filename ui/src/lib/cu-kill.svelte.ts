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
import type { DaemonEventUnion } from "./protocol";

export const cuKill = $state({
	killed: false,
});

let started = false;

function reduce(event: DaemonEventUnion): void {
	if (event.type === "cu_kill_state") {
		cuKill.killed = event.killed;
	}
}

/** Register the reducer once. Returns the unsubscribe for tests. */
export function startCuKill(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
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
