// Reactive chat store shell (TD-1004).
//
// Thin rune wrapper over the pure logic in chat-store.ts: one $state proxy
// the components read, one ChatStore mutating it, wired to the shared
// connection's event fan-out. Subscribing via onDaemonEvent (not $effect on
// lastEvent) so a burst of assistant_delta events can never drop one.

import {
  attachToSession,
  detachFromSession,
  onConnectionState,
  onDaemonEvent,
  onResume,
  sendToDaemon,
} from "./connection-status.svelte.js";
import type { NewAttachment } from "./attachments";
import { createChatState, createChatStore, showCancel, type ChatState } from "./chat-store";
import { compactNotSentCopy } from "./context-gauge";
import { notify } from "./notifications.svelte.js";
import { bindApprovals } from "./approval-store.svelte.js";
import { bindDecisions } from "./decisions.svelte.js";
import { bindMemoryProposal } from "./memory-proposal-store.svelte.js";
import { bindPlanReviews } from "./plan-review-store.svelte.js";
import { bindQuestions } from "./question-store.svelte.js";
import { bindSteeringProposal } from "./steering-proposal-store.svelte.js";
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
      bindQuestions(sessionId);
      bindPlanReviews(sessionId);
      bindMemoryProposal(sessionId);
      bindSteeringProposal(sessionId);
    },
  },
  chat,
);

let unsubscribe: (() => void) | null = null;
let unsubscribeResume: (() => void) | null = null;
let unsubscribeState: (() => void) | null = null;

/** Start following the daemon: subscribe to the event stream and ask for the
 *  session list so a session binds. Idempotent. */
export function initChat(): void {
  if (unsubscribe !== null) return;
  unsubscribe = onDaemonEvent((event) => store.applyEvent(event));
  // TD-1716: the first-token watchdog is a timer, and a suspended webview's
  // timers do not fire — so the wait is re-judged from the clock on resume.
  unsubscribeResume = onResume(() => store.resume());
  unsubscribeState = onConnectionState((state) => {
    if (state === "connected") store.reconnected();
  });
  store.refreshSessions();
}

/** Stop following and release the session attach. */
export function teardownChat(): void {
  unsubscribe?.();
  unsubscribe = null;
  unsubscribeResume?.();
  unsubscribeResume = null;
  unsubscribeState?.();
  unsubscribeState = null;
  store.dispose();
}

export const sendUserMessage: (
  text: string,
  attachments?: readonly NewAttachment[],
) => boolean = store.sendUserMessage;
export const retryLastUserMessage: () => boolean = store.retryLastUserMessage;
/** Continue a turn a restart cut off (TD-5019). */
export const continueTurn: () => boolean = store.continueTurn;
/** Save the draft and queue once typing settles (TD-5019). Call it from an
 *  effect: it reads both, so the effect re-runs on every edit. */
export const keepDraft: () => void = store.keepDraft;
export const forkFrom: (userIndex: number, content: string) => boolean = store.forkFrom;
export const setBranch: (userIndex: number, siblingIndex: number) => boolean = store.setBranch;
export const compactContext: (guidance?: string, userIndex?: number) => boolean =
  store.compactContext;
/** Compact now (TD-5109) from `/compact`, "Summarize from here" or the
 *  gauge, with a toast that says why when the request never left (TD-5154). */
export function requestCompact(guidance = "", userIndex?: number): boolean {
  if (store.compactContext(guidance, userIndex)) return true;
  notify("compact", compactNotSentCopy(showCancel(chat.turnState)));
  return false;
}
export const cancelTurn: () => boolean = store.cancelTurn;
/** Queue actions (TD-1704), bound for the queued-row controls. */
export const sendQueuedNow: (id: string) => boolean = store.sendQueuedNow;
export const editQueuedMessage: (id: string, text: string) => void = store.editQueuedMessage;
export const removeQueuedMessage: (id: string) => void = store.removeQueuedMessage;
/** Held-message actions (TD-5155): the daemon sends or drops the row. */
export const sendHeldMessage: (id: string) => boolean = store.sendHeldMessage;
export const removeHeldMessage: (id: string) => boolean = store.removeHeldMessage;
/** Rail click target (TD-1701): attach the pane to a session from the list. */
export const selectSession: (
  sessionId: string,
  turnState: SessionState["state"] | null,
) => void = store.selectSession;
