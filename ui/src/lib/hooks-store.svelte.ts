// Reactive hooks inspector store (TD-5134). Thin rune wrapper over
// hooks-store.ts, subscribed for the shell's lifetime like the Stack
// store, so a `get_hooks` reply never arrives with nobody listening.

import { onDaemonEvent, sendToDaemon } from "./connection-status.svelte.js";
import { createHooksState, createHooksStore, type HooksViewState } from "./hooks-store.js";
import { session } from "./session-status.svelte.js";

export const hooksView: HooksViewState = $state(createHooksState());

const store = createHooksStore({ send: sendToDaemon }, hooksView);

/** Shell-lifetime subscribe; returns the teardown. */
export function startHooks(): () => void {
	const off = onDaemonEvent((event) => store.applyEvent(event, session.sessionId));
	return off;
}

/** Ask the daemon for the attached session's hooks (tab open, switch). */
export function refreshHooks(): boolean {
	return store.refresh(session.sessionId);
}
