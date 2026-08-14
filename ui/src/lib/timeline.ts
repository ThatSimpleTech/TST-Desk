// Activity-timeline store (TD-1005).
//
// Pure, DOM-free transformation of daemon events into a chronological list
// of timeline entries. Kept free of runes and Tauri imports so it unit-tests
// under vitest's node environment (like splitpane.ts); the Svelte component
// wraps a Timeline instance in `$state` to get reactivity.
//
// Responsibilities:
//   - one entry per meaningful event, in seq order (AC #1)
//   - every entry carries the full payload for an expandable view (AC #2)
//   - shell_output chunks merge into the parent tool_call's live buffer (AC #4)
//   - each entry maps to a `kind` that drives distinct styling (AC #6)
//
// Assistant text (assistant_delta) is deliberately not a timeline entry: it is
// the chat conversation, not an activity record. The AC enumerates exactly the
// activity kinds shown here.

import type { DaemonEventUnion } from "./protocol";

/** Presentation category driving visually distinct treatment (AC #6). */
export type EntryKind =
  | "tool_call"
  | "tool_result"
  | "decision"
  | "tier_switch"
  | "compaction"
  | "steering_reload"
  | "error";

export interface TimelineEntry {
  /** Stable identity across re-renders: `${kind}:${seq}` (session-scoped). */
  id: string;
  kind: EntryKind;
  /** Monotonic event seq — the chronological order (AC #1). */
  seq: number;
  /** tool_call_id for entries tied to a tool call (streaming merge + diff). */
  toolCallId?: string;
  /** Short collapsed-row label. */
  title: string;
  /** One-line collapsed-row preview. */
  preview: string;
  /** Full payload for the expanded view (AC #2). */
  details: Record<string, unknown>;
  /** Live stdout buffer for shell-tool entries (AC #4). */
  stdout?: string;
  /** Live stderr buffer for shell-tool entries (AC #4). */
  stderr?: string;
}

function truncate(s: string, max = 140): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + "…";
}

/** Collapse an arguments object into a scannable one-liner. */
export function summarizeArguments(args: Record<string, unknown>): string {
  const parts = Object.entries(args);
  if (parts.length === 0) return "(no arguments)";
  return parts.map(([k, v]) => `${k}=${truncate(JSON.stringify(v))}`).join(" ");
}

/** Map one daemon event to a timeline entry, or null when it isn't shown. */
export function eventToEntry(event: DaemonEventUnion): TimelineEntry | null {
  switch (event.type) {
    case "tool_call":
      return {
        id: `tool_call:${event.seq}`,
        kind: "tool_call",
        seq: event.seq,
        toolCallId: event.tool_call_id,
        title: event.name,
        preview: summarizeArguments(event.arguments),
        details: {
          name: event.name,
          arguments: event.arguments,
          decision_class: event.decision_class ?? null,
        },
      };
    case "tool_result":
      return {
        id: `tool_result:${event.seq}`,
        kind: "tool_result",
        seq: event.seq,
        toolCallId: event.tool_call_id,
        title: event.status === "success" ? "Result" : "Error",
        preview: truncate(event.output),
        details: {
          status: event.status,
          output: event.output,
          truncated: event.truncated,
          // fs_write/fs_edit results carry a diff for syntax highlighting (AC #3)
          diff: event.diff ?? null,
        },
      };
    case "decision_logged":
      return {
        id: `decision:${event.seq}`,
        kind: "decision",
        seq: event.seq,
        title: `Class ${event.decision_class}`,
        preview: event.what,
        details: {
          decision_class: event.decision_class,
          what: event.what,
          // The rule that fired (AC #5)
          why: event.why,
          commit: event.commit,
        },
      };
    case "tier_switched":
      return {
        id: `tier_switch:${event.seq}`,
        kind: "tier_switch",
        seq: event.seq,
        title: `${event.previous ?? "default"} → ${event.tier}`,
        preview: `Routing switched to ${event.tier}`,
        details: { tier: event.tier, previous: event.previous ?? null },
      };
    case "context_compacted":
      return {
        id: `compaction:${event.seq}`,
        kind: "compaction",
        seq: event.seq,
        title: "Context compacted",
        preview: `${event.tokens_before} → ${event.tokens_after} tokens (${event.dropped_messages} dropped)`,
        details: {
          dropped_messages: event.dropped_messages,
          kept_messages: event.kept_messages,
          tokens_before: event.tokens_before,
          tokens_after: event.tokens_after,
        },
      };
    case "steering_reloaded":
      return {
        id: `steering_reload:${event.seq}`,
        kind: "steering_reload",
        seq: event.seq,
        title: "Steering reloaded",
        preview: `${event.source_count} source(s), hash ${event.prefix_hash}`,
        details: {
          prefix_hash: event.prefix_hash,
          prefix_tokens: event.prefix_tokens,
          source_count: event.source_count,
        },
      };
    case "error":
      return {
        id: `error:${event.seq}`,
        kind: "error",
        seq: event.seq,
        title: event.code,
        preview: event.message,
        details: { code: event.code, message: event.message, session_id: event.session_id ?? null },
      };
    default:
      // assistant_delta, session_state, turn_complete, cost_update,
      // boundary_update, approval_request, checkpoint_notice, ready,
      // instruction_stack, session_list — not activity entries.
      // shell_output is merged into its parent tool_call, not shown alone.
      return null;
  }
}

/** Accumulates a session's events into an ordered list of timeline entries. */
export class Timeline {
  private _entries: TimelineEntry[] = [];

  /** The chronological entries, in arrival (seq) order. */
  get entries(): readonly TimelineEntry[] {
    return this._entries;
  }

  get length(): number {
    return this._entries.length;
  }

  /** Push a validated daemon event into the timeline. */
  push(event: DaemonEventUnion): void {
    if (event.type === "shell_output") {
      this._mergeShellOutput(event);
      return;
    }
    const entry = eventToEntry(event);
    if (entry !== null) {
      this._entries.push(entry);
    }
  }

  /** Push a batch in one pass (e.g. a replayed attach window). */
  pushAll(events: Iterable<DaemonEventUnion>): void {
    for (const event of events) this.push(event);
  }

  /** Drop all entries. */
  clear(): void {
    this._entries = [];
  }

  /** Append a shell_output chunk to the live buffer of its tool_call entry. */
  private _mergeShellOutput(event: Extract<DaemonEventUnion, { type: "shell_output" }>): void {
    const target = this._findToolCall(event.tool_call_id);
    // An orphan chunk (no tool_call yet seen) carries no displayable parent;
    // drop it rather than fabricate an entry out of order.
    if (target === null) return;
    if (event.stream === "stdout") {
      target.stdout = (target.stdout ?? "") + event.chunk;
    } else {
      target.stderr = (target.stderr ?? "") + event.chunk;
    }
  }

  /** Most recent entry carrying the given tool_call_id, if any. */
  private _findToolCall(toolCallId: string): TimelineEntry | null {
    for (let i = this._entries.length - 1; i >= 0; i--) {
      if (this._entries[i].toolCallId === toolCallId) return this._entries[i];
    }
    return null;
  }
}
