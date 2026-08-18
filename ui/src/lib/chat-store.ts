// Chat pane session logic (TD-1004).
//
// Pure and rune-free so it is unit-testable in the node vitest environment;
// the reactive shell that components consume lives in chat-store.svelte.ts.
//
// The store follows one session at a time, chosen from the daemon's
// session_list — the UI never invents a session id (AGENTS §6). Which one it
// follows, and what changing it costs, live in session-binding.ts. Attaching
// replays the session's event log, so full conversation history rebuilds
// through the same reducer as live events: there is no separate history path.

import {
  toChips,
  toWireAttachments,
  type AttachmentChip,
  type NewAttachment,
} from "./attachments";
import { createMessageQueue, type QueuedMessage } from "./chat-queue";
import { createFirstTokenWait } from "./first-token-wait";
import { resetDisclosures } from "./reasoning-disclosure.svelte.js";
import { applyBind, chooseBoundSession, isTerminal } from "./session-binding";
import type {
  ClientMessageUnion,
  DaemonEventUnion,
  SessionState,
} from "./protocol";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** A reasoning model's thinking for this message (TD-1901), kept apart
   *  from `text` because it is not the answer: TD-1902 folds it behind a
   *  disclosure, and nothing may ever concatenate the two. Absent for
   *  models that do not reason. */
  reasoning?: string;
  /** Epoch ms of the first reasoning delta, so the disclosure can label
   *  itself with a duration. Cleared to a final elapsed by `reasoningMs`
   *  once content starts. */
  reasoningStartedAt?: number;
  /** How long reasoning ran, once the answer began. Undefined while it is
   *  still streaming — that is what distinguishes live from finished. */
  reasoningMs?: number;
  complete: boolean;
  /** Epoch ms when the client first saw the message (send echo or first
   *  delta). Replayed history stamps attach time — display-only (TD-1606). */
  at: number;
  /** Files sent with a user row, rendered as chips (TD-1709). Names and
   *  sizes only: the bytes are gone the moment they are on the wire, and
   *  keeping them would hold the whole session's attachments in memory for
   *  a transcript that only ever shows the label. */
  attachments?: AttachmentChip[];
  /** Tool calls that ran during this assistant turn (TD-1902). Folded
   *  like reasoning so a tool-heavy turn does not grow the transcript
   *  without bound. Absent when the turn used no tools. */
  tools?: ToolBlock[];
  /** 0-based index among user turns (TD-1708). Only on user rows. */
  userIndex?: number;
  siblingIndex?: number;
  siblingCount?: number;
}

/** One tool call on an assistant row (TD-1902). `status` is unset while
 *  the call is still running — that is what keeps the disclosure open. */
export interface ToolBlock {
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  decisionClass?: "A" | "B" | "C" | null;
  status?: "success" | "error";
  output?: string;
  errorCode?: string | null;
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
  /** The pane now shows this session, or nothing (TD-1009). Views that scope
   *  to one session rebuild here, from the replay `attach` then fetches.
   *  Optional: a pane with no activity lane alongside it is still a pane. */
  onBind?(sessionId: string | null): void;
}

export interface ChatStore {
  state: ChatState;
  applyEvent(event: DaemonEventUnion): void;
  /** Send, or queue while a turn holds the loop. Attachments ride along
   *  either way; the daemon vets them and refuses the whole message if any
   *  one fails its caps or its text test (TD-1709). */
  sendUserMessage(text: string, attachments?: readonly NewAttachment[]): boolean;
  retryLastUserMessage(): boolean;
  /** Replace a past user turn and fork from there (TD-1708). */
  forkFrom(userIndex: number, content: string): boolean;
  /** Switch to another sibling at a forked user turn (TD-1708). */
  setBranch(userIndex: number, siblingIndex: number): boolean;
  cancelTurn(): boolean;
  /** Attach the pane to a session chosen in the rail (TD-1701): detach the
   *  current one, clear the pane, attach — the replay rebuilds history
   *  through the same reducer as live events. No-op for the attached id. */
  selectSession(sessionId: string, turnState: SessionState["state"] | null): void;
  /** The window came back from suspension (TD-1716): re-decide the
   *  first-token wait from the wall clock instead of trusting a timer that
   *  was frozen through it. */
  resume(): void;
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

/** Turn-duration line (TD-1607). The wording lives in `duration.ts` so the
 *  reasoning disclosure can share it without importing this module back
 *  (TD-1902); re-exported under the old name so existing callers are
 *  untouched. */
export { formatDuration as formatTurnDuration } from "./duration";

/** The wait owns the threshold (first-token-wait.ts); the store re-exports it
 *  so the Working line's threshold and the state it describes stay one import
 *  apart for everyone who reads them together. */
export { STALL_TIMEOUT_MS } from "./first-token-wait";

export function createChatStore(deps: ChatDeps, state: ChatState = createChatState()): ChatStore {
  let nextId = 0;

  // The "Working…" wait and its watchdog (TD-1713/TD-1716). The store says
  // when a wait starts and stops, from turn evidence; the wait itself decides
  // what one in progress is worth.
  const wait = createFirstTokenWait(state);

  // Closure-level so retryLastUserMessage can call it without `this` —
  // the reactive shell re-exports these methods detached.
  /** `armWait` false hands the message over without touching the first-token
   *  clock — see the queue's send-now (TD-1704). */
  function sendUserMessageToWire(
    text: string,
    armWait = true,
    attachments: readonly NewAttachment[] = [],
  ): boolean {
    const content = text.trim();
    // TD-1709: attachments alone are a message. Empty-and-empty is not.
    if (state.sessionId === null || (content === "" && attachments.length === 0)) return false;
    // Omitted rather than sent empty when there are none (TD-1709): an
    // attachment-free send stays byte-identical to what it was before this
    // story, which is what "additive" is supposed to mean.
    const sent = deps.send(
      attachments.length === 0
        ? { type: "user_message", session_id: state.sessionId, content }
        : {
            type: "user_message",
            session_id: state.sessionId,
            content,
            attachments: toWireAttachments(attachments),
          },
    );
    if (!sent) return false;
    nextId += 1;
    const userIndex = state.messages.filter((m) => m.role === "user").length;
    state.messages.push({
      id: `m${nextId}`,
      role: "user",
      text: content,
      complete: true,
      at: Date.now(),
      attachments: attachments.length === 0 ? undefined : toChips(attachments),
      userIndex,
      siblingIndex: 0,
      siblingCount: 1,
    });
    if (!armWait) return true;
    // A fresh send restarts the wait and its watchdog even atop one already
    // in flight — the honest clock is from the latest send.
    wait.end();
    wait.begin();
    state.lastTurnDuration = null;
    return true;
  }

  const queue = createMessageQueue(state, sendUserMessageToWire, () =>
    showCancel(state.turnState),
  );

  /** Queue or send, depending on whether a turn owns the loop (TD-1704). */
  function sendUserMessage(text: string, attachments: readonly NewAttachment[] = []): boolean {
    const content = text.trim();
    if (state.sessionId === null || (content === "" && attachments.length === 0)) return false;
    if (!showCancel(state.turnState)) return sendUserMessageToWire(content, true, attachments);
    queue.add(content, attachments);
    return true;
  }

  function currentAssistant(): ChatMessage {
    const last = state.messages[state.messages.length - 1];
    if (last !== undefined && last.role === "assistant" && !last.complete) {
      return last;
    }
    nextId += 1;
    const row: ChatMessage = {
      id: `m${nextId}`,
      role: "assistant",
      text: "",
      complete: false,
      at: Date.now(),
    };
    state.messages.push(row);
    return row;
  }

  function findTool(toolCallId: string): ToolBlock | null {
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const tools = state.messages[i].tools;
      if (tools === undefined) continue;
      const found = tools.find((t) => t.toolCallId === toolCallId);
      if (found !== undefined) return found;
    }
    return null;
  }

  function sealInFlightAssistant(): void {
    const last = state.messages[state.messages.length - 1];
    if (last !== undefined && last.role === "assistant" && !last.complete) {
      last.complete = true;
      // A turn can end on reasoning alone — a cancel mid-thought, or a
      // model that thought and then only called a tool. Stamp the elapsed
      // here too, or that disclosure counts forever (TD-1901).
      if (last.reasoningStartedAt !== undefined && last.reasoningMs === undefined) {
        last.reasoningMs = Date.now() - last.reasoningStartedAt;
      }
    }
  }

  function switchSession(sessionId: string | null, turnState: SessionState["state"] | null): void {
    applyBind(state, { queue, wait, deps, onUnbind: resetDisclosures }, sessionId, turnState);
    branchSnaps.clear();
  }

  const branchSnaps = new Map<string, ChatMessage[][]>();

  function cloneMessages(): ChatMessage[] {
    return state.messages.map((m) => ({
      ...m,
      tools: m.tools?.map((t) => ({ ...t })),
    }));
  }

  function saveSnap(userIndex: number, sibling: number): void {
    const key = String(userIndex);
    const snaps = branchSnaps.get(key) ?? [];
    while (snaps.length <= sibling) snaps.push([]);
    snaps[sibling] = cloneMessages();
    branchSnaps.set(key, snaps);
  }

  return {
    state,

    applyEvent(event: DaemonEventUnion): void {
      switch (event.type) {
        case "assistant_reasoning": {
          if (event.session_id !== state.sessionId) return;
          // Reasoning is a turn in flight on exactly the terms a content
          // delta is (TD-1714), and it is the only thing a reasoning model
          // sends for whole minutes — so it ends the first-token wait too.
          // Leaving that to content is what made a working local model
          // look hung and trip the 25s stall copy (TD-1901).
          state.turnState = "running";
          wait.end();
          const last = state.messages[state.messages.length - 1];
          if (last !== undefined && last.role === "assistant" && !last.complete) {
            last.reasoning = (last.reasoning ?? "") + event.delta;
            last.reasoningStartedAt ??= Date.now();
          } else {
            nextId += 1;
            state.messages.push({
              id: `m${nextId}`,
              role: "assistant",
              text: "",
              reasoning: event.delta,
              reasoningStartedAt: Date.now(),
              complete: false,
              at: Date.now(),
            });
          }
          return;
        }
        case "assistant_delta": {
          if (event.session_id !== state.sessionId) return;
          // A delta is proof a turn is in flight (TD-1714) — the only honest
          // source of "running" the wire gives us. Replayed deltas converge
          // back through the replayed turn_complete that follows them.
          state.turnState = "running";
          // First token recovers a stalled wait: the model was slow, not gone.
          wait.end();
          const last = state.messages[state.messages.length - 1];
          if (last !== undefined && last.role === "assistant" && !last.complete) {
            // Append in place: the message object keeps its identity so the
            // keyed list never re-mounts the row while streaming.
            last.text += event.delta;
            // The first content token seals the thinking phase: stamp the
            // elapsed once, so the disclosure can stop counting (TD-1902).
            if (last.reasoningStartedAt !== undefined && last.reasoningMs === undefined) {
              last.reasoningMs = Date.now() - last.reasoningStartedAt;
            }
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
          wait.end();
          // The turn is provably over; the session itself stays alive.
          state.turnState = null;
          // Daemon-measured seconds — the duration line reports what the wire
          // said; the client never clocks turns itself (AGENTS §6).
          state.lastTurnDuration = event.duration;
          for (const [key] of branchSnaps) {
            const userIndex = Number(key);
            const row = state.messages.find((m) => m.userIndex === userIndex);
            if (row?.siblingIndex !== undefined) saveSnap(userIndex, row.siblingIndex);
          }
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
            wait.end();
          }
          if (isTerminal(event.state)) sealInFlightAssistant();
          return;
        }
        case "session_list": {
          const choice = chooseBoundSession(event.sessions, state.sessionId);
          if (choice.action === "bind") {
            switchSession(choice.sessionId, choice.turnState);
          } else if (choice.action === "unbind") {
            switchSession(null, null);
          } else if (choice.turnState !== null) {
            // Staying put, and the refresh brought a turn state worth taking.
            // A null there is the one that must not be taken (TD-1714).
            state.turnState = choice.turnState;
          }
          return;
        }
        case "error": {
          // The daemon refused a send to a dead session (TD-1711): the turn
          // will never start, so drop the waiting shimmer immediately — the
          // toast (notifications, TD-1008) carries the actionable copy.
          if (event.code !== "session_not_running") return;
          if (event.session_id != null && event.session_id !== state.sessionId) return;
          sealInFlightAssistant();
          wait.end();
          return;
        }
        case "tool_call": {
          if (event.session_id !== state.sessionId) return;
          // A tool call is a turn in flight, same as a delta (TD-1902).
          state.turnState = "running";
          wait.end();
          const row = currentAssistant();
          const tools = row.tools ?? (row.tools = []);
          if (tools.some((t) => t.toolCallId === event.tool_call_id)) return;
          tools.push({
            toolCallId: event.tool_call_id,
            name: event.name,
            arguments: event.arguments,
            decisionClass: event.decision_class,
          });
          return;
        }
        case "conversation_reset": {
          if (event.session_id !== state.sessionId) return;
          const snaps = branchSnaps.get(String(event.user_index));
          const saved = snaps?.[event.sibling_index];
          if (saved !== undefined && saved.length > 0) {
            state.messages = saved.map((m) => ({
              ...m,
              siblingIndex: m.userIndex === event.user_index ? event.sibling_index : m.siblingIndex,
              siblingCount: m.userIndex === event.user_index ? event.sibling_count : m.siblingCount,
            }));
            wait.end();
            return;
          }
          let seen = 0;
          let cut = -1;
          for (let i = 0; i < state.messages.length; i++) {
            if (state.messages[i].role !== "user") continue;
            if (seen === event.user_index) {
              cut = i;
              break;
            }
            seen += 1;
          }
          if (cut === -1) return;
          const row = state.messages[cut];
          row.text = event.content;
          row.siblingIndex = event.sibling_index;
          row.siblingCount = event.sibling_count;
          state.messages.splice(cut + 1);
          saveSnap(event.user_index, event.sibling_index);
          wait.end();
          return;
        }
        case "tool_result": {
          if (event.session_id !== state.sessionId) return;
          const block = findTool(event.tool_call_id);
          if (block === null) return;
          block.status = event.status;
          block.output = event.output;
          block.errorCode = event.error_code ?? null;
          return;
        }
        default:
          // Cost, approvals, and the rest stay the activity timeline's
          // domain (TD-1005/TD-1007).
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
        // TD-1709: the row keeps chips, not bytes, so a retry cannot resend
        // the files. Refusing is the honest answer — a silent resend without
        // them would be a different message wearing the same label.
        if (message.role !== "user") continue;
        if (message.attachments !== undefined) return false;
        if (message.userIndex === undefined) return sendUserMessageToWire(message.text);
        return this.forkFrom(message.userIndex, message.text);
      }
      return false;
    },

    forkFrom(userIndex: number, content: string): boolean {
      if (state.sessionId === null) return false;
      const text = content.trim();
      if (text === "") return false;
      const existing = branchSnaps.get(String(userIndex));
      if (existing === undefined || existing.length === 0) {
        saveSnap(userIndex, 0);
      }
      return deps.send({
        type: "fork_from",
        session_id: state.sessionId,
        user_index: userIndex,
        content: text,
      });
    },

    setBranch(userIndex: number, siblingIndex: number): boolean {
      if (state.sessionId === null) return false;
      return deps.send({
        type: "set_branch",
        session_id: state.sessionId,
        user_index: userIndex,
        sibling_index: siblingIndex,
      });
    },

    cancelTurn(): boolean {
      if (state.sessionId === null) return false;
      const sent = deps.send({ type: "cancel", session_id: state.sessionId });
      // The user said stop waiting: drop the local wait (and its watchdog)
      // immediately — the daemon's session_state remains the truth for the
      // turn itself and lands separately.
      if (sent) wait.end();
      return sent;
    },

    selectSession(sessionId: string, turnState: SessionState["state"] | null): void {
      switchSession(sessionId, turnState);
    },

    /** Resume healing, watchdog half (TD-1716) — the socket's half is the
     *  re-attach in connection-status. */
    resume: wait.resume,

    refreshSessions(): boolean {
      return deps.send({ type: "list_sessions" });
    },

    sendQueuedNow: queue.sendNow,
    editQueuedMessage: queue.edit,
    removeQueuedMessage: queue.remove,

    dispose(): void {
      if (state.sessionId !== null) deps.detach(state.sessionId);
      deps.onBind?.(null);
      state.sessionId = null;
      state.turnState = null;
      state.messages = [];
      resetDisclosures();
      queue.clear();
      wait.end();
      state.lastTurnDuration = null;
    },
  };
}
