// Apply one daemon event to the chat transcript (TD-1004).
//
// Split out of createChatStore so the store stays the wiring — send, bind,
// queue, wait — and this file is the reducer. It knows events and the
// message list; it does not own the socket.

import type { BranchSnaps } from "./chat-fork";
import { applyHeldList, clearHeldPending } from "./chat-held";
import type { ChatMessage, ChatState, ToolBlock } from "./chat-store";
import type { MessageQueue } from "./chat-queue";
import { refusedSend, returnToDraft, takeSent } from "./draft-store";
import type { FirstTokenWait } from "./first-token-wait";
import { machineOf } from "./bridged-id";
import type { DaemonEventUnion, SessionState, SessionSummary } from "./protocol";
import { sentFileFromEvent, type SentFileRef } from "./sent-files";
import { chooseBoundSession, isTerminal } from "./session-binding";
import { windowBindSessionId } from "./window-bind";

/** The bridged session the pane holds while no list has named it yet, and
 *  the last list, so a hold released by a machine_list can be re-decided
 *  without waiting for another list (TD-5015). The store sets `sessionId`
 *  on each bind; the reducer releases it. */
export interface BindHold {
  sessionId: string | null;
  lastList: readonly SessionSummary[];
}

/** Collaborators the reducer may touch. The store keeps the rest. */
export interface ChatEventContext {
  state: ChatState;
  wait: Pick<FirstTokenWait, "end">;
  queue: Pick<MessageQueue, "flushHead">;
  forks: BranchSnaps;
  allocateId: () => string;
  switchSession: (sessionId: string | null, turnState: SessionState["state"] | null) => void;
  hold: BindHold;
  /** Texts sent and not yet started or refused (TD-5023, draft-store.ts). */
  sending: string[];
  /** Write the bound session's waiting draft save now (TD-5019). */
  flushDraft: () => void;
  /** Sent files whose send_file call the pane has not seen yet, by
   *  tool_call_id (TD-5144). The store empties it on every bind. */
  earlyFiles: Map<string, SentFileRef[]>;
}

/** Re-decide the pane's session from a list and apply the choice. */
function rebindFrom(ctx: ChatEventContext, summaries: readonly SessionSummary[]): void {
  const { state } = ctx;
  const choice = chooseBoundSession(summaries, state.sessionId, windowBindSessionId(), ctx.hold.sessionId);
  if (choice.action === "bind") {
    ctx.switchSession(choice.sessionId, choice.turnState);
  } else if (choice.action === "unbind") {
    ctx.switchSession(null, null);
  } else if (choice.turnState !== null) {
    // Staying put, and the refresh brought a turn state worth taking.
    // A null there is the one that must not be taken (TD-1714).
    state.turnState = choice.turnState;
  }
}

function currentAssistant(ctx: ChatEventContext): ChatMessage {
  const last = ctx.state.messages[ctx.state.messages.length - 1];
  if (last !== undefined && last.role === "assistant" && !last.complete) {
    return last;
  }
  const row: ChatMessage = {
    id: ctx.allocateId(),
    role: "assistant",
    text: "",
    complete: false,
    at: Date.now(),
    parts: [],
  };
  ctx.state.messages.push(row);
  // Under Svelte 5's $state each path to an object is its own proxy, and a
  // write through one is seen neither through another nor through the raw
  // object (TD-5144). So: return the row as the list holds it, and the
  // helpers below write through the row and read back what it stored.
  return ctx.state.messages[ctx.state.messages.length - 1];
}

/** Append a content delta to the row's text and to its ordered parts: the
 *  trailing text segment grows, or a new one opens after a tool. */
function appendText(row: ChatMessage, delta: string): void {
  row.text += delta;
  row.parts ??= [];
  const parts = row.parts;
  const last = parts[parts.length - 1];
  if (last !== undefined && last.kind === "text") {
    last.text += delta;
  } else {
    parts.push({ kind: "text", text: delta });
  }
}

/** Append a tool call to the row's flat list and to its ordered parts. The
 *  part holds the block the list stored, so both are one object and a later
 *  result or file is visible wherever the row is read from. */
function appendTool(row: ChatMessage, block: ToolBlock): ToolBlock {
  row.tools ??= [];
  row.tools.push(block);
  const stored = row.tools[row.tools.length - 1];
  row.parts ??= [];
  row.parts.push({ kind: "tool", tool: stored });
  return stored;
}

/** Every copy of a call's block on the newest row that has it: the flat
 *  list's and the ordered part's. They are one object when appendTool made
 *  them; a row a fork restored (chat-fork.ts) can hold two proxies of one
 *  block, so a write goes to each. */
function findToolCopies(state: ChatState, toolCallId: string): ToolBlock[] {
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const { tools = [], parts = [] } = state.messages[i];
    const copies = tools.filter((t) => t.toolCallId === toolCallId);
    for (const p of parts) {
      if (p.kind !== "tool" || p.tool.toolCallId !== toolCallId) continue;
      if (!copies.includes(p.tool)) copies.push(p.tool);
    }
    if (copies.length > 0) return copies;
  }
  return [];
}

/** Add a sent file to a block once: replay on attach is idempotent. */
function addFile(block: ToolBlock, ref: SentFileRef): void {
  const files = block.files ?? [];
  if (files.some((f) => f.artifactId === ref.artifactId)) return;
  block.files = [...files, ref];
}

/** Files held for a call the pane has not seen yet, at most this many
 *  calls. A call trimmed from the log never comes, so the oldest go. */
const EARLY_FILE_CALLS = 32;

function holdEarlyFile(ctx: ChatEventContext, toolCallId: string, ref: SentFileRef): void {
  const early = ctx.earlyFiles;
  const held = early.get(toolCallId) ?? [];
  if (!held.some((f) => f.artifactId === ref.artifactId)) held.push(ref);
  early.delete(toolCallId);
  early.set(toolCallId, held);
  while (early.size > EARLY_FILE_CALLS) {
    const oldest = early.keys().next().value;
    if (oldest === undefined) break;
    early.delete(oldest);
  }
}

function sealInFlightAssistant(state: ChatState): void {
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

export function applyChatEvent(ctx: ChatEventContext, event: DaemonEventUnion): void {
  const { state, wait } = ctx;
  switch (event.type) {
    case "user_turn": {
      if (event.session_id !== state.sessionId) return;
      // The daemon took one of our sends: the composer it emptied is saved
      // now, not after the debounce (TD-5019).
      if (takeSent(ctx.sending, event.content)) ctx.flushDraft();
      const already = state.messages.find(
        (m) => m.role === "user" && m.turnId === event.turn_id,
      );
      if (already !== undefined) return;
      // A new turn, from this pane or another device: the cut one is no
      // longer the last, so its Continue goes (TD-5019).
      state.lastTurnError = null;
      const last = state.messages[state.messages.length - 1];
      if (
        last !== undefined &&
        last.role === "user" &&
        last.turnId === undefined &&
        last.text === event.content
      ) {
        last.turnId = event.turn_id;
        return;
      }
      const userIndex = state.messages.filter((m) => m.role === "user").length;
      state.messages.push({
        id: ctx.allocateId(),
        role: "user",
        text: event.content,
        complete: true,
        at: Date.now(),
        userIndex,
        siblingIndex: 0,
        siblingCount: 1,
        turnId: event.turn_id,
      });
      return;
    }
    case "queued_messages": {
      if (event.session_id !== state.sessionId) return;
      applyHeldList(state, ctx.sending, event);
      return;
    }
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
        state.messages.push({
          id: ctx.allocateId(),
          role: "assistant",
          text: "",
          reasoning: event.delta,
          reasoningStartedAt: Date.now(),
          complete: false,
          at: Date.now(),
          parts: [],
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
        appendText(last, event.delta);
        // The first content token seals the thinking phase: stamp the
        // elapsed once, so the disclosure can stop counting (TD-1902).
        if (last.reasoningStartedAt !== undefined && last.reasoningMs === undefined) {
          last.reasoningMs = Date.now() - last.reasoningStartedAt;
        }
      } else {
        state.messages.push({
          id: ctx.allocateId(),
          role: "assistant",
          text: event.delta,
          complete: false,
          at: Date.now(),
          parts: [{ kind: "text", text: event.delta }],
        });
      }
      return;
    }
    case "turn_complete": {
      if (event.session_id !== state.sessionId) return;
      sealInFlightAssistant(state);
      wait.end();
      // The turn is provably over; the session itself stays alive.
      state.turnState = null;
      // Daemon-measured seconds — the duration line reports what the wire
      // said; the client never clocks turns itself (AGENTS §6).
      state.lastTurnDuration = event.duration;
      state.lastTurnError = event.error_code ?? null;
      ctx.forks.saveActive(state);
      // The loop is free: the queue's head becomes the next turn (TD-1704).
      // Only here, never on a terminal session_state — the daemon refuses
      // sends to a dead session, so flushing into one would void the text.
      // Not after a turn a restart cut off either (TD-5018): the rows were
      // queued behind a reply that never came, and sending one would answer
      // for the user. They wait for Continue or a send of their own.
      if (event.error_code !== "interrupted_by_restart") ctx.queue.flushHead();
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
      if (isTerminal(event.state)) sealInFlightAssistant(state);
      return;
    }
    case "session_list": {
      const { hold } = ctx;
      hold.lastList = event.sessions;
      if (hold.sessionId !== null && event.sessions.some((s) => s.session_id === hold.sessionId)) {
        hold.sessionId = null;
      }
      rebindFrom(ctx, event.sessions);
      return;
    }
    case "machine_list": {
      // A held session whose computer was just removed will never be listed:
      // let go now, since the list that dropped its rows may already be in.
      const { hold } = ctx;
      const mch = machineOf(hold.sessionId);
      if (mch === null || event.machines.some((m) => m.id === mch)) return;
      hold.sessionId = null;
      rebindFrom(ctx, hold.lastList);
      return;
    }
    case "error": {
      // The daemon refused a send to a dead session (TD-1711), or held it
      // while it drains for a restart (TD-5023): the turn will never start,
      // so drop the waiting shimmer immediately — the toast (notifications,
      // TD-1008) carries the actionable copy.
      if (event.in_reply_to === "send_queued" || event.in_reply_to === "remove_queued") {
        // A held row's verb was refused (TD-5155): the row stays held, and
        // no composer text is involved. The toast says why.
        if (event.session_id != null && event.session_id !== state.sessionId) return;
        clearHeldPending(state.held);
        if (event.in_reply_to === "send_queued") wait.end();
        return;
      }
      if (event.code !== "session_not_running" && event.code !== "daemon_draining") return;
      if (event.session_id != null && event.session_id !== state.sessionId) return;
      wait.end();
      const refused = refusedSend(ctx.sending);
      if (event.code === "session_not_running") {
        sealInFlightAssistant(state);
        return;
      }
      // A drain holds new turns while running ones finish, so a reply may
      // still be streaming here: it is not sealed. The text goes back to
      // the composer to send again once the restart is done (TD-5019).
      if (refused !== null) returnToDraft(state, refused);
      return;
    }
    case "tool_call": {
      if (event.session_id !== state.sessionId) return;
      // A tool call is a turn in flight, same as a delta (TD-1902).
      state.turnState = "running";
      wait.end();
      const row = currentAssistant(ctx);
      if (row.tools?.some((t) => t.toolCallId === event.tool_call_id)) return;
      const block = appendTool(row, {
        toolCallId: event.tool_call_id,
        name: event.name,
        arguments: event.arguments,
        decisionClass: event.decision_class,
      });
      // Files that reached the pane before their call land now.
      const early = ctx.earlyFiles.get(event.tool_call_id);
      if (early !== undefined) {
        ctx.earlyFiles.delete(event.tool_call_id);
        for (const ref of early) {
          if (ref.sessionId === state.sessionId) addFile(block, ref);
        }
      }
      return;
    }
    case "conversation_reset": {
      if (event.session_id !== state.sessionId) return;
      if (!ctx.forks.applyReset(state, event)) return;
      wait.end();
      return;
    }
    case "tool_result": {
      if (event.session_id !== state.sessionId) return;
      for (const block of findToolCopies(state, event.tool_call_id)) {
        block.status = event.status;
        block.output = event.output;
        block.errorCode = event.error_code ?? null;
      }
      return;
    }
    case "artifact_ready": {
      // A file the agent sent lands on the send_file call that sent it
      // (TD-5144). A TD-3201 artifact is the Artifacts pane's alone.
      if (event.session_id !== state.sessionId) return;
      const ref = sentFileFromEvent(event, state.sessionId);
      if (ref === null || !event.tool_call_id) return;
      const copies = findToolCopies(state, event.tool_call_id);
      // No call yet: hold the file for it. If the call was trimmed from the
      // log it never comes, and the Artifacts pane still lists the file.
      if (copies.length === 0) {
        holdEarlyFile(ctx, event.tool_call_id, ref);
        return;
      }
      for (const block of copies) addFile(block, ref);
      return;
    }
    case "verify_result":
      // Timeline only (TD-4204). Never a second assistant bubble.
      return;
    default:
      // Cost, approvals, and the rest stay the activity timeline's
      // domain (TD-1005/TD-1007).
      return;
  }
}
