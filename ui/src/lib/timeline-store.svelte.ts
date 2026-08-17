// Reactive activity-timeline store (TD-1005).
//
// The thin runes layer over the pure `Timeline` in timeline.ts. `timeline.ts`
// is DOM/runes-free so it unit-tests under vitest's node environment; this
// module injects a `$state` array as the Timeline's storage, so appends and
// in-place shell-buffer writes invalidate through the reactive proxy with no
// re-sync step (mirrors chat-store.svelte.ts).

import { Timeline, type TimelineEntry } from './timeline';
import type { DaemonEventUnion } from './protocol';

export const entries = $state<TimelineEntry[]>([]);

const timeline = new Timeline(entries);

/** Append one validated daemon event to the timeline. Events belonging to
 *  any session but the bound one are dropped (TD-1009). */
export function push(event: DaemonEventUnion): void {
  timeline.push(event);
}

/** Point the pane at a session, or at nothing (TD-1009).
 *
 *  Called by the chat store, which owns the attach/detach pairing: binding
 *  the pane and re-hydrating this list from the attach replay are the same
 *  moment. Null unbinds — no session, no activity. */
export function bindSession(sessionId: string | null): void {
  timeline.bind(sessionId);
}

/** Drop all timeline entries, keeping the bound session. */
export function clear(): void {
  timeline.clear();
}
