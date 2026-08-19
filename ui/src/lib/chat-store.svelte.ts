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
import type { NewAttachment } from "./attachments";
import { createChatState, createChatStore, type ChatState } from "./chat-store";
import { bindApprovals } from "./approval-store.svelte.js";
import { bindDecisions } from "./decisions.svelte.js";
import { bindSession } from "./timeline-store.svelte.js";
import type { SessionState } from "./protocol";

export const chat: ChatState = $state(createChatState());

const store = createChatStore(
  {
    send: sendToDaemon,
    attach: attachToSession,
    detach: detachFromSession,
    // The activity timeline, Files pane, and decisions list show one
    // session (TD-1009 / TD-1203). The pane's binding is this store's
    // to declare; both views clear-and-rebuild from the attach replay.
    onBind: (sessionId) => {
      bindSession(sessionId);
      bindDecisions(sessionId);
      bindApprovals(sessionId);
    },
  },
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

export const sendUserMessage: (
  text: string,
  attachments?: readonly NewAttachment[],
) => boolean = store.sendUserMessage;
export const retryLastUserMessage: () => boolean = store.retryLastUserMessage;
export const forkFrom: (userIndex: number, content: string) => boolean = store.forkFrom;
export const setBranch: (userIndex: number, siblingIndex: number) => boolean = store.setBranch;
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
