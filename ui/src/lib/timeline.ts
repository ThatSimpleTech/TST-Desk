// Activity-timeline store (TD-1005).
//
// Pure, DOM-free transformation of daemon events into a chronological list
// of timeline entries. Kept free of runes and Tauri imports so it unit-tests
// under vitest's node environment (like splitpane.ts). Reactivity comes from
// storage injection: timeline-store.svelte.ts constructs the Timeline over a
// `$state` array, so every write below flows through the reactive proxy —
// appends invalidate the list, and in-place shell-buffer writes invalidate
// the entry. Tests just pass a plain array (or nothing).
//
// Responsibilities:
//   - one entry per meaningful event, in seq order (AC #1)
//   - every entry carries the full payload for an expandable view (AC #2)
//   - shell_output chunks merge into the parent tool_call's live buffer (AC #4)
//   - each entry maps to a `kind` that drives distinct styling (AC #6)
//   - one bound session's activity and no other's (TD-1009)
//
// Assistant text (assistant_delta) is deliberately not a timeline entry: it is
// the chat conversation, not an activity record. The AC enumerates exactly the
// activity kinds shown here.

import type { DaemonEventUnion, SessionState, TurnComplete, UserTurn } from "./protocol";

/** Presentation category driving visually distinct treatment (AC #6).
 *  `turn` is the group header the other kinds sit under: one per user
 *  message, closed by the daemon's turn_complete. */
export type EntryKind =
  | "turn"
  | "tool_call"
  | "tool_result"
  | "decision"
  | "approval"
  | "tier_switch"
  | "compaction"
  | "steering_reload"
  | "error"
  | "verify";

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
        title:
          event.error_code === "approval_denied"
            ? "Denied"
            : event.status === "success"
              ? "Result"
              : "Error",
        preview: truncate(event.output),
        details: {
          status: event.status,
          error_code: event.error_code ?? null,
          output: event.output,
          truncated: event.truncated,
          // fs_write/fs_edit results carry a diff for syntax highlighting (AC #3)
          diff: event.diff ?? null,
        },
      };
    case "approval_request":
      return {
        id: `approval:${event.seq}`,
        kind: "approval",
        seq: event.seq,
        toolCallId: event.tool_call_id,
        title: event.summary,
        preview: `${event.tool_name} · Class ${event.decision_class}`,
        details: {
          tool_name: event.tool_name,
          arguments: event.arguments,
          decision_class: event.decision_class,
          reason: event.reason,
          // Flipped to "approved"/"denied" when the resolving tool_result lands.
          status: "pending",
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
    case "verify_result":
      return {
        id: `verify:${event.seq}`,
        kind: "verify",
        seq: event.seq,
        title: event.pending ? "Verify pending" : `Verify ${event.verdict}`,
        preview: event.summary,
        details: {
          verdict: event.verdict,
          summary: event.summary,
          cost: event.cost,
          pending: event.pending ?? false,
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
          steering_tokens: event.steering_tokens,
          source_count: event.source_count,
        },
      };
    case "rule_activated":
      return {
        id: `rule_activated:${event.seq}`,
        kind: "steering_reload",
        seq: event.seq,
        title: "Rule activated",
        preview: event.rule_path,
        details: {
          rule_path: event.rule_path,
          session_id: event.session_id,
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
      // assistant_delta, cost_update, boundary_update, checkpoint_notice,
      // ready, instruction_stack, session_list — not activity entries.
      // shell_output is merged into its parent tool_call, not shown alone.
      // user_turn, turn_complete and session_state are turn boundaries the
      // Timeline folds itself (see `turnEntry`), since numbering a turn
      // needs the count so far.
      return null;
  }
}

/** Collapse a user message to one scannable line for a turn header. */
function oneLine(text: string, max = 120): string {
  return truncate(text.replace(/\s+/g, " ").trim(), max);
}

/** The header row that opens a turn: the user's message, numbered by the
 *  timeline as it goes. `status` and the measurements start empty and are
 *  filled by the daemon's turn_complete — the UI never times or prices a
 *  turn itself (AGENTS §6). */
export function turnEntry(event: UserTurn, number: number): TimelineEntry {
  return {
    id: `turn:${event.seq}`,
    kind: "turn",
    seq: event.seq,
    title: `Turn ${number}`,
    preview: oneLine(event.content),
    details: {
      turn_id: event.turn_id,
      number,
      // running → complete | failed (turn_complete), or cancelled |
      // interrupted when the session stops with the turn still open.
      status: "running",
      duration: null,
      cost: null,
      tokens: null,
      tier: null,
      error_code: null,
    },
  };
}

/**
 * The session an event belongs to, or null when it names none.
 *
 * A daemon error is written straight to the socket rather than through a
 * session's event log (core/tstd/protocol.py `build_error`), so it may carry
 * no session at all — and an event that names no session is not this
 * session's activity. Those surface through TD-1008's notification lane,
 * which is where a daemon-level failure belongs.
 */
function eventSessionId(event: DaemonEventUnion): string | null {
  if (!("session_id" in event)) return null;
  return event.session_id ?? null;
}

/** Accumulates a session's events into an ordered list of timeline entries. */
export class Timeline {
  private _entries: TimelineEntry[];
  /** The session whose activity this timeline shows; null when unbound. */
  private _sessionId: string | null = null;
  /** Highest log position already folded in — see `push`. */
  private _lastSeq = 0;
  /** Turns opened so far; the next header takes the next number. */
  private _turns = 0;

  /** Storage is injected so the caller can supply a reactive (`$state`) array. */
  constructor(storage: TimelineEntry[] = []) {
    this._entries = storage;
  }

  /** The bound session, or null. */
  get sessionId(): string | null {
    return this._sessionId;
  }

  /**
   * Show a different session's activity (TD-1009).
   *
   * The daemon's event log is the record, so a switch drops what the previous
   * session left here and lets the bind's attach replay rebuild the new one.
   * The window keeps no second copy of a history it would then have to hold
   * in step — the pane is a view of the log, not a store of it.
   *
   * Re-binding the session already shown is a no-op, deliberately: an attach
   * that replays only the gap (a reconnect, TD-1716's resume) would otherwise
   * empty the pane with nothing coming back to refill it.
   */
  bind(sessionId: string | null): void {
    if (sessionId === this._sessionId) return;
    this._sessionId = sessionId;
    this.clear();
  }

  /** The chronological entries, in arrival (seq) order. */
  get entries(): readonly TimelineEntry[] {
    return this._entries;
  }

  get length(): number {
    return this._entries.length;
  }

  /**
   * Push a validated daemon event into the timeline.
   *
   * One connection carries every session the window follows, so an event
   * belonging to another one is not this pane's activity and is dropped
   * (TD-1009). Within the session, `seq` is the log position: an attach
   * replays from a requested seq, so an event at or below what we already
   * folded is that replay handing back an entry already on screen, not a
   * second occurrence of it. Dropping it here means no re-attach can double
   * count, whatever the client's own duplicate detection did or did not do.
   *
   * A daemon error carries no seq — it bypasses the event log — so there is
   * nothing to compare it against and nothing that can replay it.
   */
  push(event: DaemonEventUnion): void {
    if (this._sessionId === null || eventSessionId(event) !== this._sessionId) return;
    const seq: unknown = event.seq;
    if (typeof seq === "number") {
      if (seq <= this._lastSeq) return;
      this._lastSeq = seq;
    }
    if (event.type === "shell_output") {
      this._mergeShellOutput(event);
      return;
    }
    // Turn boundaries: the user's message opens a group, turn_complete
    // closes it with the daemon's own duration and cost, and a session that
    // stops with a turn still open settles that turn as stopped.
    if (event.type === "user_turn") {
      this._turns += 1;
      this._entries.push(turnEntry(event, this._turns));
      return;
    }
    if (event.type === "turn_complete") {
      this._closeTurn(event);
      return;
    }
    if (event.type === "session_state") {
      this._settleTurn(event.state);
      return;
    }
    const entry = eventToEntry(event);
    if (entry !== null) {
      this._entries.push(entry);
    }
    // A tool_result resolves the pending approval for its tool call: reflect
    // the choice on the approval entry so resolved cards show what was chosen
    // (TD-1007 AC #6).
    if (event.type === "tool_result") {
      const approval = this._findApproval(event.tool_call_id);
      if (approval !== null) {
        approval.details.status = event.error_code === "approval_denied" ? "denied" : "approved";
      }
    }
  }

  /** Push a batch in one pass (e.g. a replayed attach window). */
  pushAll(events: Iterable<DaemonEventUnion>): void {
    for (const event of events) this.push(event);
  }

  /** Drop all entries and the log position they were folded from. Mutates in
   *  place — reassigning would detach injected reactive storage. */
  clear(): void {
    this._entries.length = 0;
    this._lastSeq = 0;
    this._turns = 0;
  }

  /** Fill the open turn's header with what the daemon measured. A
   *  turn_complete with no header above it (an attach window that opened
   *  mid-turn) has nothing to annotate and is dropped. */
  private _closeTurn(event: TurnComplete): void {
    const turn = this._findTurn();
    if (turn === null) return;
    turn.details.status = event.failed ? "failed" : "complete";
    turn.details.duration = event.duration;
    turn.details.cost = event.cost;
    turn.details.tokens = event.tokens;
    turn.details.tier = event.tier;
    turn.details.error_code = event.error_code;
  }

  /** A session that stops mid-turn leaves the header saying "running"
   *  forever unless the stop is written onto it. Only a still-running turn
   *  is touched: once turn_complete has spoken, the state event is old news. */
  private _settleTurn(state: SessionState["state"]): void {
    if (state !== "cancelled" && state !== "interrupted" && state !== "failed") return;
    const turn = this._findTurn();
    if (turn === null || turn.details.status !== "running") return;
    turn.details.status = state;
  }

  /** Most recent turn header, if any. */
  private _findTurn(): TimelineEntry | null {
    for (let i = this._entries.length - 1; i >= 0; i--) {
      if (this._entries[i].kind === "turn") return this._entries[i];
    }
    return null;
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

  /** Most recent approval entry for the given tool_call_id, if any. */
  private _findApproval(toolCallId: string): TimelineEntry | null {
    for (let i = this._entries.length - 1; i >= 0; i--) {
      const e = this._entries[i];
      if (e.kind === "approval" && e.toolCallId === toolCallId) return e;
    }
    return null;
  }
}
