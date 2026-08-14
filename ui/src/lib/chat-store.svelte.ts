// Reactive chat store shell (TD-1004).
//
// Thin rune wrapper over the pure logic in chat-store.ts: one $state proxy
// the components read, one ChatStore mutating it, wired to the shared
// connection's event fan-out. Subscribing via onDaemonEvent (not $effect on
// lastEvent) so a burst of assistant_delta events can never drop one.

import {
  attachToSession,
  detachFromSession,
  onDaemonEvent,
  sendToDaemon,
} from "./connection-status.svelte.js";
import { createChatState, createChatStore, type ChatState } from "./chat-store";

export const chat: ChatState = $state(createChatState());

const store = createChatStore(
  { send: sendToDaemon, attach: attachToSession, detach: detachFromSession },
  chat,
);

let unsubscribe: (() => void) | null = null;

/** Start following the daemon: subscribe to the event stream and ask for the
 *  session list so a session binds. Idempotent. */
export function initChat(): void {
  if (unsubscribe !== null) return;
  unsubscribe = onDaemonEvent((event) => store.applyEvent(event));
  store.refreshSessions();
}

/** Stop following and release the session attach. */
export function teardownChat(): void {
  unsubscribe?.();
  unsubscribe = null;
  store.dispose();
}

export const sendUserMessage: (text: string) => boolean = store.sendUserMessage;
export const retryLastUserMessage: () => boolean = store.retryLastUserMessage;
export const cancelTurn: () => boolean = store.cancelTurn;
