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

import { toChips, toWireAttachments, type NewAttachment } from "./attachments";
import { isBridged } from "./bridged-id";
import type { ChatMessage } from "./chat-message";
import { applyChatEvent, type BindHold } from "./chat-events";
import { createBranchSnaps } from "./chat-fork";
import type { HeldMessage } from "./chat-held";
import { createMessageQueue, type QueuedMessage } from "./chat-queue";
import { createDraftStore, noteSent, type DraftStore } from "./draft-store";
import { createFirstTokenWait } from "./first-token-wait";
import { resetDisclosures } from "./reasoning-disclosure.svelte.js";
import { applyBind } from "./session-binding";
import type { SentFileRef } from "./sent-files";
import type {
  ClientMessageUnion,
  DaemonEventUnion,
  SessionState,
} from "./protocol";

export type { ChatMessage, ChatPart, ToolBlock } from "./chat-message";

export interface ChatState {
  sessionId: string | null;
  turnState: SessionState["state"] | null;
  messages: ChatMessage[];
  /** Messages typed while a turn was live (TD-1704), drained one per turn
   *  end so the tail stays editable. See chat-queue.ts for why they wait
   *  here instead of going straight to the daemon's own queue. */
  queued: QueuedMessage[];
  /** Messages a restart left held (TD-5155): the daemon's latest
   *  `queued_messages` list. They wait for Send or Remove; see chat-held.ts. */
  held: HeldMessage[];
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
  /** The last turn_complete's error_code, cleared with lastTurnDuration and
   *  when another turn starts. "interrupted_by_restart" is a turn the daemon
   *  sealed after a restart cut it off, and the pane offers Continue for it
   *  (TD-5019). */
  lastTurnError: string | null;
  /** The composer's text. Here rather than in the pane so each session keeps
   *  its own and a send the daemon refuses can come back to it (TD-5019). */
  draft: string;
}

export interface ChatDeps {
  send(msg: ClientMessageUnion): boolean;
  attach(sessionId: string): void;
  detach(sessionId: string): void;
  /** The pane now shows this session, or nothing (TD-1009). Views that scope
   *  to one session rebuild here, from the replay `attach` then fetches.
   *  Optional: a pane with no activity lane alongside it is still a pane. */
  onBind?(sessionId: string | null): void;
  /** Where drafts outlive the window (TD-5019). Defaults to localStorage. */
  drafts?: DraftStore;
}

export interface ChatStore {
  state: ChatState;
  applyEvent(event: DaemonEventUnion): void;
  /** Send, or queue while a turn holds the loop. Attachments ride along
   *  either way; the daemon vets them and refuses the whole message if any
   *  one fails its caps or its text test (TD-1709). */
  sendUserMessage(text: string, attachments?: readonly NewAttachment[]): boolean;
  retryLastUserMessage(): boolean;
  /** Pick up a turn a restart cut off (TD-5019): an ordinary "Continue"
   *  message the user clicked, through the same path as any other send. */
  continueTurn(): boolean;
  /** The draft or the queue changed: save them once typing settles. */
  keepDraft(): void;
  /** Replace a past user turn and fork from there (TD-1708). */
  forkFrom(userIndex: number, content: string): boolean;
  /** Switch to another sibling at a forked user turn (TD-1708). */
  setBranch(userIndex: number, siblingIndex: number): boolean;
  /** Compact now (TD-5109): everything before the latest user turn, or
   *  from `userIndex` on. Refused locally while a turn is live; the daemon
   *  refuses too and says why. */
  compactContext(guidance?: string, userIndex?: number): boolean;
  cancelTurn(): boolean;
  /** Attach the pane to a session chosen in the rail (TD-1701): detach the
   *  current one, clear the pane, attach — the replay rebuilds history
   *  through the same reducer as live events. No-op for the attached id. */
  selectSession(sessionId: string, turnState: SessionState["state"] | null): void;
  /** The window came back from suspension (TD-1716): re-decide the
   *  first-token wait from the wall clock instead of trusting a timer that
   *  was frozen through it. */
  resume(): void;
  /** The socket handshook again. The daemon may have restarted, and a
   *  restarted hub lists no other computer's rows until its links report,
   *  so a followed remote session is held until a list names it (TD-5015). */
  reconnected(): void;
  refreshSessions(): boolean;
  /** Hand one queued row to the daemon now, ahead of the rows before it
   *  (TD-1704) — the steer. It leaves the local queue either way. */
  sendQueuedNow(id: string): boolean;
  /** Replace a queued row's text in place, keeping its id and position. */
  editQueuedMessage(id: string, text: string): void;
  /** Drop a queued row without ever sending it. */
  removeQueuedMessage(id: string): void;
  /** Ask the daemon to send a held message (TD-5155). The row stays until
   *  the daemon's next list drops it. */
  sendHeldMessage(id: string): boolean;
  /** Ask the daemon to drop a held message unsent (TD-5155). */
  removeHeldMessage(id: string): boolean;
  dispose(): void;
}

export function createChatState(): ChatState {
  return {
    sessionId: null,
    turnState: null,
    messages: [],
    queued: [],
    held: [],
    awaitingFirstToken: false,
    turnStalled: false,
    awaitingSince: null,
    lastTurnDuration: null,
    lastTurnError: null,
    draft: "",
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

/** The composer sends only when a session is bound and the socket is live,
 *  and, for another computer's session, while that computer is connected
 *  (TD-5015; `machineUp` is always true for this computer's own). */
export function canSend(sessionId: string | null, wsState: string, machineUp: boolean = true): boolean {
  return sessionId !== null && wsState === "connected" && machineUp;
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
  const drafts = deps.drafts ?? createDraftStore();
  // Texts on their way to the daemon, until it starts or refuses each one.
  const sending: string[] = [];

  /** Write the bound session's waiting draft save now, not after the debounce. */
  function flushDraft(): void {
    if (state.sessionId !== null) drafts.flush(state.sessionId);
  }

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
    noteSent(sending, content);
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
    state.lastTurnError = null;
    return true;
  }

  const queue = createMessageQueue(state, sendUserMessageToWire, () =>
    showCancel(state.turnState),
  );

  /** One verb on one held row. Marked pending until the daemon's next list,
   *  so a second click cannot ask twice. */
  function actOnHeld(type: "send_queued" | "remove_queued", id: string): boolean {
    const row = state.held.find((r) => r.id === id);
    if (state.sessionId === null || row === undefined || row.pending) return false;
    const sent = deps.send({ type, session_id: state.sessionId, queued_id: id });
    if (!sent) return false;
    row.pending = true;
    if (type === "send_queued" && !showCancel(state.turnState)) {
      // It starts a turn now, like a send from the composer (TD-1714).
      wait.end();
      wait.begin();
      state.lastTurnDuration = null;
      state.lastTurnError = null;
    }
    return true;
  }

  /** Queue or send, depending on whether a turn owns the loop (TD-1704). */
  function sendUserMessage(text: string, attachments: readonly NewAttachment[] = []): boolean {
    const content = text.trim();
    if (state.sessionId === null || (content === "" && attachments.length === 0)) return false;
    if (!showCancel(state.turnState)) return sendUserMessageToWire(content, true, attachments);
    queue.add(content, attachments);
    return true;
  }

  const forks = createBranchSnaps();
  const hold: BindHold = { sessionId: null, lastList: [] };
  // Sent files that arrived before their call (chat-events.ts). They belong
  // to the bound session, so a bind lets them go with its rows.
  const earlyFiles = new Map<string, SentFileRef[]>();

  function switchSession(sessionId: string | null, turnState: SessionState["state"] | null): void {
    const moving = state.sessionId !== sessionId;
    // Saved before the bind clears the queue, so the rows leave with it.
    if (moving) flushDraft();
    applyBind(state, { queue, wait, deps, onUnbind: resetDisclosures }, sessionId, turnState);
    forks.clear();
    if (moving) {
      earlyFiles.clear();
      sending.length = 0;
      state.draft = sessionId === null ? "" : drafts.restore(sessionId);
    }
    // A session on another computer that the last list didn't name (a new
    // one made there just now) is held until a list does (TD-5015).
    const listed = hold.lastList.some((s) => s.session_id === sessionId);
    hold.sessionId = isBridged(sessionId) && !listed ? sessionId : null;
  }

  // A plain function, not a method: the .svelte.ts wrapper exports the
  // store's methods detached, so `this` is undefined when Retry calls it.
  function forkFrom(userIndex: number, content: string): boolean {
    if (state.sessionId === null) return false;
    const text = content.trim();
    if (text === "") return false;
    if (!forks.has(userIndex)) forks.save(userIndex, 0, state.messages);
    const sent = deps.send({
      type: "fork_from",
      session_id: state.sessionId,
      user_index: userIndex,
      content: text,
    });
    if (sent) noteSent(sending, text);
    return sent;
  }

  const eventCtx = {
    state,
    wait,
    queue,
    forks,
    allocateId: () => {
      nextId += 1;
      return `m${nextId}`;
    },
    switchSession,
    hold,
    sending,
    flushDraft,
    earlyFiles,
  };

  return {
    state,

    applyEvent(event: DaemonEventUnion): void {
      applyChatEvent(eventCtx, event);
    },

    sendUserMessage,

    /** Retry (TD-1606 / TD-1708): fork the last user turn in place so ‹ ›
     *  can reach the original. Refused while a turn is live. */
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
        return forkFrom(message.userIndex, message.text);
      }
      return false;
    },

    continueTurn(): boolean {
      return sendUserMessage("Continue");
    },

    keepDraft(): void {
      if (state.sessionId !== null) drafts.schedule(state.sessionId, state.draft, state.queued);
    },

    forkFrom,

    setBranch(userIndex: number, siblingIndex: number): boolean {
      if (state.sessionId === null) return false;
      return deps.send({
        type: "set_branch",
        session_id: state.sessionId,
        user_index: userIndex,
        sibling_index: siblingIndex,
      });
    },

    compactContext(guidance = "", userIndex?: number): boolean {
      if (state.sessionId === null || showCancel(state.turnState)) return false;
      const text = guidance.trim();
      return deps.send({
        type: "compact_context",
        session_id: state.sessionId,
        ...(text !== "" ? { guidance: text } : {}),
        ...(userIndex !== undefined ? { user_index: userIndex } : {}),
      });
    },

    cancelTurn(): boolean {
      if (state.sessionId === null) return false;
      // Stop this turn only. `cancel` still ends the session, and nothing
      // in the composer sends it (TD-5003).
      const sent = deps.send({ type: "stop_turn", session_id: state.sessionId });
      // The user said stop waiting: drop the local wait (and its watchdog)
      // immediately — the daemon's turn_complete remains the truth for the
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

    reconnected(): void {
      if (isBridged(state.sessionId)) hold.sessionId = state.sessionId;
    },

    refreshSessions(): boolean {
      return deps.send({ type: "list_sessions" });
    },

    sendQueuedNow: queue.sendNow,
    editQueuedMessage: queue.edit,
    removeQueuedMessage: queue.remove,

    sendHeldMessage: (id: string) => actOnHeld("send_queued", id),
    removeHeldMessage: (id: string) => actOnHeld("remove_queued", id),

    dispose(): void {
      flushDraft();
      if (state.sessionId !== null) deps.detach(state.sessionId);
      deps.onBind?.(null);
      state.sessionId = null;
      hold.sessionId = null;
      state.turnState = null;
      state.messages = [];
      state.held = [];
      resetDisclosures();
      queue.clear();
      wait.end();
      state.lastTurnDuration = null;
      state.lastTurnError = null;
      state.draft = "";
      sending.length = 0;
      earlyFiles.clear();
    },
  };
}
