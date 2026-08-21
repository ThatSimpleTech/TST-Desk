// Computer-use kill-switch chrome (TD-3404).
//
// The latch is process-wide on the daemon. This store does not invent it:
// it starts not-killed (the daemon default) and only flips when
// `cu_kill_state` arrives. Title bar, palette, and the shortcut all send
// `set_cu_kill`; they do not keep a second copy of the flag.

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

/** Ask the daemon to engage or clear the switch. */
export function setCuKill(killed: boolean): void {
	sendToDaemon({ type: "set_cu_kill", killed });
}

export function resetCuKill(): void {
	cuKill.killed = false;
	started = false;
}
