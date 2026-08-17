// Chat pane session logic (TD-1004).
//
// Pure and rune-free so it is unit-testable in the node vitest environment;
// the reactive shell that components consume lives in chat-store.svelte.ts.
//
// The store follows one session at a time, chosen from the daemon's
// session_list — the UI never invents a session id (AGENTS §6). Attaching
// replays the session's event log, so full conversation history rebuilds
// through the same reducer as live events: there is no separate history path.

import { createMessageQueue, type QueuedMessage } from "./chat-queue";
import type {
  ClientMessageUnion,
  DaemonEventUnion,
  SessionState,
} from "./protocol";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  complete: boolean;
  /** Epoch ms when the client first saw the message (send echo or first
   *  delta). Replayed history stamps attach time — display-only (TD-1606). */
  at: number;
}

export interface ChatState {
  sessionId: string | null;
  turnState: SessionState["state"] | null;
  messages: ChatMessage[];
  /** Messages typed while a turn was live (TD-1704), drained one per turn
   *  end so the tail stays editable. See chat-queue.ts for why they wait
   *  here instead of going straight to the daemon's own queue. */
  queued: QueuedMessage[];
  /** Set between a user send and the first assistant_delta — the "Working…"
   *  shimmer's window (TD-1607). Only a local send arms it (TD-1714): the
   *  daemon's "running" means the session loop is alive, not that a turn is
   *  in flight, so binding must never fabricate a wait from it. */
  awaitingFirstToken: boolean;
  /** First-token watchdog tripped (TD-1713): 25s without a delta or a
   *  terminal turn event. The Working shimmer swaps to honest "no response
   *  yet" copy until the first delta recovers it. */
  turnStalled: boolean;
  /** Epoch ms when the current first-token wait began — the basis of the
   *  Working line's elapsed counter (TD-1713). Null when not waiting. */
  awaitingSince: number | null;
  /** Seconds the last turn took, as measured by the daemon on turn_complete.
   *  The UI never times turns itself (AGENTS §6). Cleared on the next send. */
  lastTurnDuration: number | null;
}

export interface ChatDeps {
  send(msg: ClientMessageUnion): boolean;
  attach(sessionId: string): void;
  detach(sessionId: string): void;
}

export interface ChatStore {
  state: ChatState;
  applyEvent(event: DaemonEventUnion): void;
  sendUserMessage(text: string): boolean;
  retryLastUserMessage(): boolean;
  cancelTurn(): boolean;
  /** Attach the pane to a session chosen in the rail (TD-1701): detach the
   *  current one, clear the pane, attach — the replay rebuilds history
   *  through the same reducer as live events. No-op for the attached id. */
  selectSession(sessionId: string, turnState: SessionState["state"] | null): void;
  refreshSessions(): boolean;
  /** Hand one queued row to the daemon now, ahead of the rows before it
   *  (TD-1704) — the steer. It leaves the local queue either way. */
  sendQueuedNow(id: string): boolean;
  /** Replace a queued row's text in place, keeping its id and position. */
  editQueuedMessage(id: string, text: string): void;
  /** Drop a queued row without ever sending it. */
  removeQueuedMessage(id: string): void;
  dispose(): void;
}

export function createChatState(): ChatState {
  return {
    sessionId: null,
    turnState: null,
    messages: [],
    queued: [],
    awaitingFirstToken: false,
    turnStalled: false,
    awaitingSince: null,
    lastTurnDuration: null,
  };
}

/** Enter submits, Shift+Enter newlines — the composer's only key rule. */
export function shouldSubmit(key: string, shiftKey: boolean): boolean {
  return key === "Enter" && !shiftKey;
}

/** A turn is live while running or parked awaiting approval (superset of the
 *  criterion's "running": awaiting_approval is still an in-flight turn). */
export function showCancel(turnState: SessionState["state"] | null): boolean {
  return turnState === "running" || turnState === "awaiting_approval";
}

/** The composer sends only when a session is bound and the socket is live. */
export function canSend(sessionId: string | null, wsState: string): boolean {
  return sessionId !== null && wsState === "connected";
}

/** Turn-duration line (TD-1607): a sub-second turn still reads "1s" — "0s"
 *  would claim work didn't happen. */
export function formatTurnDuration(seconds: number): string {
  const total = Math.max(1, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}m ${rest}s`;
}

/** First-token watchdog (TD-1713): a wait this long with no assistant_delta
 *  and no terminal turn event stops claiming "Working" and says so. Long
 *  enough that a cold model on a big prompt stays unflagged; short enough
 *  that the 2026-08-14 silent stall could not sit an hour unremarked. */
export const STALL_TIMEOUT_MS = 25_000;

/** Terminal turn states seal any in-flight assistant message so it does not
 *  show a streaming cursor forever. */
function isTerminal(state: SessionState["state"]): boolean {
  return (
    state === "complete" ||
    state === "failed" ||
    state === "cancelled" ||
    state === "interrupted"
  );
}

export function createChatStore(deps: ChatDeps, state: ChatState = createChatState()): ChatStore {
  let nextId = 0;

  // First-token watchdog (TD-1713). One outstanding timer per store; every
  // transition out of the first-token wait clears it.
  let stallTimer: ReturnType<typeof setTimeout> | null = null;

  function armStallWatchdog(): void {
    if (stallTimer !== null) clearTimeout(stallTimer);
    stallTimer = setTimeout(() => {
      stallTimer = null;
      if (state.awaitingFirstToken) state.turnStalled = true;
    }, STALL_TIMEOUT_MS);
    // Under node/vitest the timer is a Timeout object; unref so a pending
    // watchdog never holds a test process open. Browsers return a number.
    (stallTimer as unknown as { unref?: () => void }).unref?.();
  }

  /** Begin waiting if not already: stamps the elapsed basis and arms the
   *  watchdog. A wait already in progress keeps its original stamp — a
   *  replayed "running" must not restart the user's clock. */
  function startFirstTokenWait(): void {
    if (state.awaitingFirstToken) return;
    state.awaitingFirstToken = true;
    state.turnStalled = false;
    state.awaitingSince = Date.now();
    armStallWatchdog();
  }

  function endFirstTokenWait(): void {
    state.awaitingFirstToken = false;
    state.turnStalled = false;
    state.awaitingSince = null;
    if (stallTimer !== null) {
      clearTimeout(stallTimer);
      stallTimer = null;
    }
  }

  // Closure-level so retryLastUserMessage can call it without `this` —
  // the reactive shell re-exports these methods detached.
  /** `armWait` false hands the message over without touching the first-token
   *  clock — see the queue's send-now (TD-1704). */
  function sendUserMessageToWire(text: string, armWait = true): boolean {
    const content = text.trim();
    if (state.sessionId === null || content === "") return false;
    const sent = deps.send({ type: "user_message", session_id: state.sessionId, content });
    if (!sent) return false;
    nextId += 1;
    state.messages.push({ id: `m${nextId}`, role: "user", text: content, complete: true, at: Date.now() });
    if (!armWait) return true;
    // A fresh send restarts the wait and its watchdog even atop one already
    // in flight — the honest clock is from the latest send.
    endFirstTokenWait();
    startFirstTokenWait();
    state.lastTurnDuration = null;
    return true;
  }

  const queue = createMessageQueue(state, sendUserMessageToWire, () =>
    showCancel(state.turnState),
  );

  /** Queue or send, depending on whether a turn owns the loop (TD-1704). */
  function sendUserMessage(text: string): boolean {
    const content = text.trim();
    if (state.sessionId === null || content === "") return false;
    if (!showCancel(state.turnState)) return sendUserMessageToWire(content);
    queue.add(content);
    return true;
  }

  function sealInFlightAssistant(): void {
    const last = state.messages[state.messages.length - 1];
    if (last !== undefined && last.role === "assistant" && !last.complete) {
      last.complete = true;
    }
  }

  function switchSession(sessionId: string | null, turnState: SessionState["state"] | null): void {
    if (state.sessionId === sessionId) return;
    if (state.sessionId !== null) deps.detach(state.sessionId);
    state.sessionId = sessionId;
    // TD-1714: a summary's "running" means the session's loop is alive — the
    // daemon sets it once at open and it spans the session's whole life — not
    // that a turn is in flight. Mapping it straight into turnState locked the
    // composer behind the stop morph on every live bind. Only turn evidence
    // (a delta, an approval round-trip) may raise "running"; the attach
    // replay re-derives it through the reducer below.
    state.turnState = turnState === "running" ? null : turnState;
    state.messages = [];
    // Queued text belongs to the session it was composed against; carrying it
    // across would deliver it to a conversation that never asked for it.
    queue.clear();
    endFirstTokenWait();
    state.lastTurnDuration = null;
    if (sessionId !== null) deps.attach(sessionId);
  }

  return {
    state,

    applyEvent(event: DaemonEventUnion): void {
      switch (event.type) {
        case "assistant_delta": {
          if (event.session_id !== state.sessionId) return;
          // A delta is proof a turn is in flight (TD-1714) — the only honest
          // source of "running" the wire gives us. Replayed deltas converge
          // back through the replayed turn_complete that follows them.
          state.turnState = "running";
          // First token recovers a stalled wait: the model was slow, not gone.
          endFirstTokenWait();
          const last = state.messages[state.messages.length - 1];
          if (last !== undefined && last.role === "assistant" && !last.complete) {
            // Append in place: the message object keeps its identity so the
            // keyed list never re-mounts the row while streaming.
            last.text += event.delta;
          } else {
            nextId += 1;
            state.messages.push({
              id: `m${nextId}`,
              role: "assistant",
              text: event.delta,
              complete: false,
              at: Date.now(),
            });
          }
          return;
        }
        case "turn_complete": {
          if (event.session_id !== state.sessionId) return;
          sealInFlightAssistant();
          endFirstTokenWait();
          // The turn is provably over; the session itself stays alive.
          state.turnState = null;
          // Daemon-measured seconds — the duration line reports what the wire
          // said; the client never clocks turns itself (AGENTS §6).
          state.lastTurnDuration = event.duration;
          // The loop is free: the queue's head becomes the next turn (TD-1704).
          // Only here, never on a terminal session_state — the daemon refuses
          // sends to a dead session, so flushing into one would void the text.
          queue.flushHead();
          return;
        }
        case "session_state": {
          if (event.session_id !== state.sessionId) return;
          if (event.state === "running") {
            // TD-1714: "running" reports the session loop is alive — emitted
            // once at open and replayed on every attach — not that a turn is
            // in flight. It may corroborate existing turn evidence (an
            // approval just resolved, deltas are streaming, our send awaits
            // its first token) but must never fabricate a turn or a wait by
            // itself, and it must never cut a wait our send started.
            const last = state.messages[state.messages.length - 1];
            const turnLive =
              state.awaitingFirstToken ||
              state.turnState === "awaiting_approval" ||
              (last !== undefined && last.role === "assistant" && !last.complete);
            state.turnState = turnLive ? "running" : null;
          } else {
            state.turnState = event.state;
            endFirstTokenWait();
          }
          if (isTerminal(event.state)) sealInFlightAssistant();
          return;
        }
        case "session_list": {
          const summaries = event.sessions;
          if (summaries.length === 0) {
            switchSession(null, null);
            return;
          }
          // Keep the current session while the daemon still lists it; else
          // bind the most recently updated one.
          if (summaries.some((s) => s.session_id === state.sessionId)) {
            const current = summaries.find((s) => s.session_id === state.sessionId);
            // TD-1714: a summary's "running" is session-liveness, not turn
            // evidence — a refresh must never stamp it over the local state
            // (neither raising a phantom turn nor standing down a real one).
            if (current !== undefined && current.state !== "running") state.turnState = current.state;
            return;
          }
          // Auto-bind liveness (TD-1711): a terminal session can never run
          // another turn (the daemon refuses its user_message with
          // session_not_running). Binding one would strand the composer on
          // a corpse — e.g. the post-restart auto-adopt of an interrupted
          // tombstone observed 2026-08-14. With nothing live, stay unbound:
          // the empty state points at the rail's New Session.
          const live = summaries.filter((s) => !isTerminal(s.state));
          if (live.length === 0) {
            switchSession(null, null);
            return;
          }
          const newest = live.reduce((a, b) => (a.updated_at >= b.updated_at ? a : b));
          switchSession(newest.session_id, newest.state);
          return;
        }
        case "error": {
          // The daemon refused a send to a dead session (TD-1711): the turn
          // will never start, so drop the waiting shimmer immediately — the
          // toast (notifications, TD-1008) carries the actionable copy.
          if (event.code !== "session_not_running") return;
          if (event.session_id != null && event.session_id !== state.sessionId) return;
          sealInFlightAssistant();
          endFirstTokenWait();
          return;
        }
        default:
          // Tool activity, cost, approvals: the activity timeline's domain
          // (TD-1005/TD-1007), not the conversation's.
          return;
      }
    },

    sendUserMessage,

    /** Retry (TD-1606): resend the last user message verbatim over the same
     *  user_message wire message, refused while a turn is live. Today's
     *  protocol has no edit/fork, so the resend appends a new row — that
     *  duplication is the honest record. */
    retryLastUserMessage(): boolean {
      if (showCancel(state.turnState)) return false;
      for (let i = state.messages.length - 1; i >= 0; i--) {
        const message = state.messages[i];
        if (message.role === "user") return sendUserMessageToWire(message.text);
      }
      return false;
    },

    cancelTurn(): boolean {
      if (state.sessionId === null) return false;
      const sent = deps.send({ type: "cancel", session_id: state.sessionId });
      // The user said stop waiting: drop the local wait (and its watchdog)
      // immediately — the daemon's session_state remains the truth for the
      // turn itself and lands separately.
      if (sent) endFirstTokenWait();
      return sent;
    },

    selectSession(sessionId: string, turnState: SessionState["state"] | null): void {
      switchSession(sessionId, turnState);
    },

    refreshSessions(): boolean {
      return deps.send({ type: "list_sessions" });
    },

    sendQueuedNow: queue.sendNow,
    editQueuedMessage: queue.edit,
    removeQueuedMessage: queue.remove,

    dispose(): void {
      if (state.sessionId !== null) deps.detach(state.sessionId);
      state.sessionId = null;
      state.turnState = null;
      state.messages = [];
      queue.clear();
      endFirstTokenWait();
      state.lastTurnDuration = null;
    },
  };
}
