// Disclosure state for a message's reasoning block (TD-1902).
//
// Module-level rather than component-level because `MessageList` windows its
// rows (@tanstack/svelte-virtual): a row scrolled out of view is destroyed,
// so component state would reset every time it came back — and the rows are
// keyed by virtual index, not by message id, so a recycled component could
// carry another message's fold state. Holding it here, keyed by id, makes
// both cases impossible rather than unlikely.
//
// Reactive (`$state`) so the component can read the rule directly in a
// `$derived` and re-render on a toggle, with no local mirror to drift.
// Rules stay pure functions over a narrow input, so they unit-test without
// a DOM or a component.
//
// Two things decide whether a block is open, in this order:
//
//   1. An explicit toggle by the user. It wins forever after — a reader who
//      opened a thought to read it should not have it shut under them by the
//      first content token arriving.
//   2. Otherwise: open while the thinking is the live thing, closed once the
//      answer has started. Reasoning is the only sign of life a thinking
//      model gives for minutes, and it is noise the moment it isn't.

// Never import from ./chat-store here: it imports this module for
// resetDisclosures, and the cycle leaves one side undefined at init.
import { formatDuration } from "./duration";

/** Explicit user toggles, keyed by message id. Absent = never touched. */
const toggles = $state<Record<string, boolean>>({});

/** The subset of a chat message this module reasons about. Narrow on
 *  purpose: it keeps the rules testable without building a whole message. */
export interface ReasoningState {
  id: string;
  reasoning?: string;
  /** Set once the answer begins, or once the turn is sealed. Undefined
   *  means the thinking is still streaming. */
  reasoningMs?: number;
}

export function hasReasoning(message: ReasoningState): boolean {
  return message.reasoning !== undefined && message.reasoning !== "";
}

/** True while this message's thinking is still arriving. */
export function isThinkingLive(message: ReasoningState): boolean {
  return hasReasoning(message) && message.reasoningMs === undefined;
}

export function isExpanded(message: ReasoningState): boolean {
  const explicit = toggles[message.id];
  if (explicit !== undefined) return explicit;
  return isThinkingLive(message);
}

/** Record a user toggle. Returns the new state so callers can avoid a
 *  second lookup. */
export function toggle(message: ReasoningState): boolean {
  const next = !isExpanded(message);
  toggles[message.id] = next;
  return next;
}

/** Drop remembered toggles. Called when the pane rebinds to another
 *  session: message ids restart at m1, so a stale entry would otherwise
 *  decide the disclosure state of an unrelated message. */
export function resetDisclosures(): void {
  for (const id of Object.keys(toggles)) delete toggles[id];
}

/** The disclosure's label. Live thinking has no duration to report yet —
 *  the elapsed clock belongs to the Working line, which is still running
 *  at that point, and two clocks disagreeing by a frame reads as a bug. */
export function thoughtLabel(message: ReasoningState): string {
  if (message.reasoningMs === undefined) return "Thinking";
  return `Thought for ${formatDuration(message.reasoningMs / 1000)}`;
}

// ── Tool-call folds (TD-1902 remaining criterion) ──────────────────────
//
// Same toggle map, different key. A tool-heavy turn would otherwise dump
// every call into the transcript; the disclosure is the resting state,
// open only while the call has no result yet.
//
// "No result yet" is not enough on its own. A restart that cuts a call off
// seals the turn with turn_complete and never logs a tool_result for it,
// live or on replay, so the status stays unset for good. The row says
// which: a call on a finished (complete) row will never run again, so it
// is never live, whatever tool it is (TD-5144).

export interface ToolFoldState {
  toolCallId: string;
  name: string;
  status?: "success" | "error";
  /** The call's arguments; a delegate or workflow fold names its job from them. */
  arguments?: Record<string, unknown>;
}

function toolKey(messageId: string, toolCallId: string): string {
  return `${messageId}:tool:${toolCallId}`;
}

/** True while the call runs: no result yet and its row's turn not over.
 *  `rowComplete` is required so no caller can forget the row. */
export function isToolLive(block: ToolFoldState, rowComplete: boolean): boolean {
  return !rowComplete && block.status === undefined;
}

export function isToolExpanded(
  messageId: string,
  block: ToolFoldState,
  rowComplete: boolean,
): boolean {
  const explicit = toggles[toolKey(messageId, block.toolCallId)];
  if (explicit !== undefined) return explicit;
  return isToolLive(block, rowComplete);
}

export function toggleTool(messageId: string, block: ToolFoldState, rowComplete: boolean): boolean {
  const next = !isToolExpanded(messageId, block, rowComplete);
  toggles[toolKey(messageId, block.toolCallId)] = next;
  return next;
}

/** How much of a delegate's task its fold shows when it names no agent. */
const TASK_LABEL_CHARS = 40;

/** What a worker call is doing, for its fold (TD-5195): "3 tasks", an
 *  agent's name, or the start of the task. "" for any other tool. */
function toolDetail(block: ToolFoldState): string {
  const args = block.arguments ?? {};
  if (block.name === "workflow" && Array.isArray(args.tasks)) {
    const n = args.tasks.length;
    return n === 1 ? "1 task" : `${n} tasks`;
  }
  if (block.name !== "delegate") return "";
  if (typeof args.agent === "string" && args.agent.trim() !== "") return args.agent.trim();
  if (typeof args.task !== "string") return "";
  const task = args.task.replace(/\s+/g, " ").trim();
  return task.length > TASK_LABEL_CHARS ? `${task.slice(0, TASK_LABEL_CHARS).trimEnd()}…` : task;
}

export function toolLabel(block: ToolFoldState): string {
  const name = block.status === "error" ? `${block.name} failed` : block.name;
  const detail = toolDetail(block);
  return detail === "" ? name : `${name} · ${detail}`;
}
