// Apply one daemon event to the chat transcript (TD-1004).
//
// Split out of createChatStore so the store stays the wiring — send, bind,
// queue, wait — and this file is the reducer. It knows events and the
// message list; it does not own the socket.

import type { BranchSnaps } from "./chat-fork";
import type { ChatMessage, ChatState, ToolBlock } from "./chat-store";
import type { MessageQueue } from "./chat-queue";
import type { FirstTokenWait } from "./first-token-wait";
import type { DaemonEventUnion, SessionState } from "./protocol";
import { chooseBoundSession, isTerminal } from "./session-binding";

/** Collaborators the reducer may touch. The store keeps the rest. */
export interface ChatEventContext {
  state: ChatState;
  wait: Pick<FirstTokenWait, "end">;
  queue: Pick<MessageQueue, "flushHead">;
  forks: BranchSnaps;
  allocateId: () => string;
  switchSession: (sessionId: string | null, turnState: SessionState["state"] | null) => void;
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
  };
  ctx.state.messages.push(row);
  return row;
}

function findTool(state: ChatState, toolCallId: string): ToolBlock | null {
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const tools = state.messages[i].tools;
    if (tools === undefined) continue;
    const found = tools.find((t) => t.toolCallId === toolCallId);
    if (found !== undefined) return found;
  }
  return null;
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
      const already = state.messages.find(
        (m) => m.role === "user" && m.turnId === event.turn_id,
      );
      if (already !== undefined) return;
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
        state.messages.push({
          id: ctx.allocateId(),
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
      sealInFlightAssistant(state);
      wait.end();
      // The turn is provably over; the session itself stays alive.
      state.turnState = null;
      // Daemon-measured seconds — the duration line reports what the wire
      // said; the client never clocks turns itself (AGENTS §6).
      state.lastTurnDuration = event.duration;
      ctx.forks.saveActive(state);
      // The loop is free: the queue's head becomes the next turn (TD-1704).
      // Only here, never on a terminal session_state — the daemon refuses
      // sends to a dead session, so flushing into one would void the text.
      ctx.queue.flushHead();
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
      const choice = chooseBoundSession(event.sessions, state.sessionId);
      if (choice.action === "bind") {
        ctx.switchSession(choice.sessionId, choice.turnState);
      } else if (choice.action === "unbind") {
        ctx.switchSession(null, null);
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
      sealInFlightAssistant(state);
      wait.end();
      return;
    }
    case "tool_call": {
      if (event.session_id !== state.sessionId) return;
      // A tool call is a turn in flight, same as a delta (TD-1902).
      state.turnState = "running";
      wait.end();
      const row = currentAssistant(ctx);
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
      if (!ctx.forks.applyReset(state, event)) return;
      wait.end();
      return;
    }
    case "tool_result": {
      if (event.session_id !== state.sessionId) return;
      const block = findTool(state, event.tool_call_id);
      if (block === null) return;
      block.status = event.status;
      block.output = event.output;
      block.errorCode = event.error_code ?? null;
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
