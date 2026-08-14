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

/** Append one validated daemon event to the timeline. */
export function push(event: DaemonEventUnion): void {
  timeline.push(event);
}

/** Drop all timeline entries. */
export function clear(): void {
  timeline.clear();
}
