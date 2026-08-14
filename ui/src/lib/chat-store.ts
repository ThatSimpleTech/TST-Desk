// Chat pane session logic (TD-1004).
//
// Pure and rune-free so it is unit-testable in the node vitest environment;
// the reactive shell that components consume lives in chat-store.svelte.ts.
//
// The store follows one session at a time, chosen from the daemon's
// session_list — the UI never invents a session id (AGENTS §6). Attaching
// replays the session's event log, so full conversation history rebuilds
// through the same reducer as live events: there is no separate history path.

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
  /** Set between a user send and the first assistant_delta — the "Working…"
   *  shimmer's window (TD-1607). Also true while attached to a session the
   *  daemon reports as running but no delta has arrived yet. */
  awaitingFirstToken: boolean;
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
  dispose(): void;
}

export function createChatState(): ChatState {
  return {
    sessionId: null,
    turnState: null,
    messages: [],
    awaitingFirstToken: false,
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

  // Closure-level so retryLastUserMessage can call it without `this` —
  // the reactive shell re-exports these methods detached.
  function sendUserMessageToWire(text: string): boolean {
    const content = text.trim();
    if (state.sessionId === null || content === "") return false;
    const sent = deps.send({ type: "user_message", session_id: state.sessionId, content });
    if (!sent) return false;
    nextId += 1;
    state.messages.push({ id: `m${nextId}`, role: "user", text: content, complete: true, at: Date.now() });
    state.awaitingFirstToken = true;
    state.lastTurnDuration = null;
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
    state.turnState = turnState;
    state.messages = [];
    // Attaching to a session mid-turn shows the shimmer until the replayed
    // (or live) deltas arrive; anything else is at rest.
    state.awaitingFirstToken = turnState === "running";
    state.lastTurnDuration = null;
    if (sessionId !== null) deps.attach(sessionId);
  }

  return {
    state,

    applyEvent(event: DaemonEventUnion): void {
      switch (event.type) {
        case "assistant_delta": {
          if (event.session_id !== state.sessionId) return;
          state.awaitingFirstToken = false;
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
          state.awaitingFirstToken = false;
          // Daemon-measured seconds — the duration line reports what the wire
          // said; the client never clocks turns itself (AGENTS §6).
          state.lastTurnDuration = event.duration;
          return;
        }
        case "session_state": {
          if (event.session_id !== state.sessionId) return;
          state.turnState = event.state;
          if (event.state === "running") {
            // A replayed "running" can land after replayed deltas — don't
            // resurrect the shimmer over an actively streaming message.
            const last = state.messages[state.messages.length - 1];
            state.awaitingFirstToken =
              last === undefined || last.role !== "assistant" || last.complete;
          } else {
            state.awaitingFirstToken = false;
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
            if (current !== undefined) state.turnState = current.state;
            return;
          }
          const newest = summaries.reduce((a, b) => (a.updated_at >= b.updated_at ? a : b));
          switchSession(newest.session_id, newest.state);
          return;
        }
        default:
          // Tool activity, cost, approvals: the activity timeline's domain
          // (TD-1005/TD-1007), not the conversation's.
          return;
      }
    },

    sendUserMessage: sendUserMessageToWire,

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
      return deps.send({ type: "cancel", session_id: state.sessionId });
    },

    selectSession(sessionId: string, turnState: SessionState["state"] | null): void {
      switchSession(sessionId, turnState);
    },

    refreshSessions(): boolean {
      return deps.send({ type: "list_sessions" });
    },

    dispose(): void {
      if (state.sessionId !== null) deps.detach(state.sessionId);
      state.sessionId = null;
      state.turnState = null;
      state.messages = [];
      state.awaitingFirstToken = false;
      state.lastTurnDuration = null;
    },
  };
}
