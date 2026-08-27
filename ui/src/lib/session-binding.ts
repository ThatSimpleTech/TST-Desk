// Which session the chat pane follows (TD-1711, TD-1714, TD-1715, TD-1009).
//
// Split along the pure/effectful line, on purpose. Choosing is a function of
// the daemon's session_list and the id the pane holds right now: every rule a
// defect has already paid for lives there, decidable without a store, a socket
// or a timer. Applying is the other half — the teardown and re-attach a change
// of session costs, which needs the store's collaborators and nothing else.
//
// The store keeps the wiring: it reads the choice and calls the effect.

import type { MessageQueue } from "./chat-queue";
import type { ChatDeps, ChatState } from "./chat-store";
import type { FirstTokenWait } from "./first-token-wait";
import type { SessionState, SessionSummary } from "./protocol";

/** Terminal turn states. The binding rule reads this to refuse a session that
 *  can never run another turn; the store reads it to seal an assistant message
 *  that will get no more deltas. */
export function isTerminal(state: SessionState["state"]): boolean {
  return (
    state === "complete" ||
    state === "failed" ||
    state === "cancelled" ||
    state === "interrupted"
  );
}

/** What a session_list means for the pane. On `keep`, `turnState` is the
 *  refreshed state worth stamping — null when the summary carries nothing that
 *  may overwrite what the pane already knows about the turn (TD-1714). */
export type SessionBinding =
  | { action: "keep"; turnState: SessionState["state"] | null }
  | { action: "unbind" }
  | { action: "bind"; sessionId: string; turnState: SessionState["state"] | null };

/** The store's collaborators, as much of each as a bind is allowed to touch.
 *  Not a slice of ChatState the way first-token-wait takes one: what a bind
 *  needs beyond the state is the queue, the wait and the transport. */
export interface BindContext {
  queue: Pick<MessageQueue, "clear">;
  wait: Pick<FirstTokenWait, "end">;
  deps: Pick<ChatDeps, "attach" | "detach" | "onBind">;
  /** Side effects that belong to the session that just left (TD-1902
   *  clears reasoning-disclosure toggles here — message ids restart at
   *  m1, so a stale toggle would fold an unrelated row). */
  onUnbind?: () => void;
}

/** The whole auto-bind policy, in one place and free of effects. */
export function chooseBoundSession(
  summaries: readonly SessionSummary[],
  currentId: string | null,
  windowBind: string | null = null,
): SessionBinding {
  if (windowBind !== null) {
    const bound = summaries.find((s) => s.session_id === windowBind);
    if (bound === undefined) {
      if (currentId === windowBind) {
        return { action: "keep", turnState: null };
      }
      return { action: "unbind" };
    }
    if (bound.archived || isTerminal(bound.state)) {
      return { action: "unbind" };
    }
    if (currentId === windowBind) {
      return {
        action: "keep",
        turnState: bound.state === "running" ? null : bound.state,
      };
    }
    return {
      action: "bind",
      sessionId: windowBind,
      turnState: bound.state === "running" ? null : bound.state,
    };
  }
  // Keep the current session while the daemon still lists it *and* still files
  // it under the default view; else bind the most recently updated one.
  // Archiving the bound session (TD-1715) is therefore a rebind, not a pane
  // left pointing at a filed-away conversation the rail no longer shows.
  const current = summaries.find((s) => s.session_id === currentId);
  if (current !== undefined && !current.archived) {
    // TD-1714: a summary's "running" is session-liveness, not turn evidence —
    // a refresh must never stamp it over the local state (neither raising a
    // phantom turn nor standing down a real one).
    return { action: "keep", turnState: current.state === "running" ? null : current.state };
  }
  // Auto-bind liveness (TD-1711): a terminal session can never run another
  // turn (the daemon refuses its user_message with session_not_running).
  // Binding one would strand the composer on a corpse — e.g. the post-restart
  // auto-adopt of an interrupted tombstone observed 2026-08-14. With nothing
  // live, stay unbound: the empty state points at the rail's New Session.
  // Archived sessions never win auto-bind (TD-1715): the user filed them away,
  // so adopting one would undo that on the next refresh.
  const live = summaries.filter((s) => !isTerminal(s.state) && !s.archived);
  if (live.length === 0) return { action: "unbind" };
  const newest = live.reduce((a, b) => (a.updated_at >= b.updated_at ? a : b));
  return { action: "bind", sessionId: newest.session_id, turnState: newest.state };
}

/** Point the pane at `sessionId`, or at nothing. Every change of the pane's
 *  session goes through here, whoever decided it. */
export function applyBind(
  state: ChatState,
  ctx: BindContext,
  sessionId: string | null,
  turnState: SessionState["state"] | null,
): void {
  if (state.sessionId === sessionId) return;
  if (state.sessionId !== null) ctx.deps.detach(state.sessionId);
  state.sessionId = sessionId;
  // TD-1714: a summary's "running" means the session's loop is alive — the
  // daemon sets it once at open and it spans the session's whole life — not
  // that a turn is in flight. Mapping it straight into turnState locked the
  // composer behind the stop morph on every live bind. Only turn evidence
  // (a delta, an approval round-trip) may raise "running"; the attach replay
  // re-derives it through the store's reducer.
  state.turnState = turnState === "running" ? null : turnState;
  state.messages = [];
  ctx.onUnbind?.();
  // Queued text belongs to the session it was composed against; carrying it
  // across would deliver it to a conversation that never asked for it.
  ctx.queue.clear();
  ctx.wait.end();
  state.lastTurnDuration = null;
  // The activity lane scopes to the same session (TD-1009). Bind it before
  // the attach, so the replay the attach fetches lands in a list already
  // pointing at the session it describes.
  ctx.deps.onBind?.(sessionId);
  if (sessionId !== null) ctx.deps.attach(sessionId);
}
