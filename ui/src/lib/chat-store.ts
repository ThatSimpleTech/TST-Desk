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
}

export interface ChatState {
  sessionId: string | null;
  turnState: SessionState["state"] | null;
  messages: ChatMessage[];
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
  cancelTurn(): boolean;
  refreshSessions(): boolean;
  dispose(): void;
}

export function createChatState(): ChatState {
  return { sessionId: null, turnState: null, messages: [] };
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
    if (sessionId !== null) deps.attach(sessionId);
  }

  return {
    state,

    applyEvent(event: DaemonEventUnion): void {
      switch (event.type) {
        case "assistant_delta": {
          if (event.session_id !== state.sessionId) return;
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
            });
          }
          return;
        }
        case "turn_complete": {
          if (event.session_id !== state.sessionId) return;
          sealInFlightAssistant();
          return;
        }
        case "session_state": {
          if (event.session_id !== state.sessionId) return;
          state.turnState = event.state;
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

    sendUserMessage(text: string): boolean {
      const content = text.trim();
      if (state.sessionId === null || content === "") return false;
      const sent = deps.send({ type: "user_message", session_id: state.sessionId, content });
      if (!sent) return false;
      nextId += 1;
      state.messages.push({ id: `m${nextId}`, role: "user", text: content, complete: true });
      return true;
    },

    cancelTurn(): boolean {
      if (state.sessionId === null) return false;
      return deps.send({ type: "cancel", session_id: state.sessionId });
    },

    refreshSessions(): boolean {
      return deps.send({ type: "list_sessions" });
    },

    dispose(): void {
      if (state.sessionId !== null) deps.detach(state.sessionId);
      state.sessionId = null;
      state.turnState = null;
      state.messages = [];
    },
  };
}
