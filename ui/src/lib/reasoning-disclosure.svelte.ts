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
