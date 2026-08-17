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
  onResume,
  sendToDaemon,
} from "./connection-status.svelte.js";
import { createChatState, createChatStore, type ChatState } from "./chat-store";
import type { SessionState } from "./protocol";

export const chat: ChatState = $state(createChatState());

const store = createChatStore(
  { send: sendToDaemon, attach: attachToSession, detach: detachFromSession },
  chat,
);

let unsubscribe: (() => void) | null = null;
let unsubscribeResume: (() => void) | null = null;

/** Start following the daemon: subscribe to the event stream and ask for the
 *  session list so a session binds. Idempotent. */
export function initChat(): void {
  if (unsubscribe !== null) return;
  unsubscribe = onDaemonEvent((event) => store.applyEvent(event));
  // TD-1716: the first-token watchdog is a timer, and a suspended webview's
  // timers do not fire — so the wait is re-judged from the clock on resume.
  unsubscribeResume = onResume(() => store.resume());
  store.refreshSessions();
}

/** Stop following and release the session attach. */
export function teardownChat(): void {
  unsubscribe?.();
  unsubscribe = null;
  unsubscribeResume?.();
  unsubscribeResume = null;
  store.dispose();
}

export const sendUserMessage: (text: string) => boolean = store.sendUserMessage;
export const retryLastUserMessage: () => boolean = store.retryLastUserMessage;
export const cancelTurn: () => boolean = store.cancelTurn;
/** Queue actions (TD-1704), bound for the queued-row controls. */
export const sendQueuedNow: (id: string) => boolean = store.sendQueuedNow;
export const editQueuedMessage: (id: string, text: string) => void = store.editQueuedMessage;
export const removeQueuedMessage: (id: string) => void = store.removeQueuedMessage;
/** Rail click target (TD-1701): attach the pane to a session from the list. */
export const selectSession: (
  sessionId: string,
  turnState: SessionState["state"] | null,
) => void = store.selectSession;
