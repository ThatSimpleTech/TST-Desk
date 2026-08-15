// Reactive instruction-stack store (TD-1201).
//
// Thin rune wrapper over the pure logic in stack-store.ts: one $state the
// StackPanel reads, one StackStore mutating it, wired to the shared
// connection's event fan-out. The loop already pushes a fresh stack on
// every steering hot reload, so live updates arrive via applyEvent — no
// polling.

import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";
import { session } from "./session-status.svelte.js";
import { createStackState, createStackStore, type StackState } from "./stack-store.js";

/** The stack for the attached session, empty until the first one lands. */
export const stack: StackState = $state(createStackState());

const store = createStackStore({ send: sendToDaemon }, stack);

let unsubscribe: (() => void) | null = null;

/** Subscribe to daemon events. Idempotent; pair with teardownStack. */
export function initStack(): void {
	if (unsubscribe !== null) return;
	unsubscribe = onDaemonEvent((event) => store.applyEvent(event, session.sessionId));
}

export function teardownStack(): void {
	unsubscribe?.();
	unsubscribe = null;
}

/** Ask the daemon for the attached session's stack (panel open, switch). */
export function refreshStack(): boolean {
	return store.refresh(session.sessionId);
}
