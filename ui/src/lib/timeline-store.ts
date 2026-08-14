// Reactive activity-timeline store (TD-1005).
//
// The thin runes layer over the pure `Timeline` in timeline.ts. `timeline.ts`
// is DOM/runes-free so it unit-tests under vitest's node environment; this
// module re-exposes its state as a reactive `entries` list the component can
// bind to, and `push`/`clear` for the wiring to feed events into (mirrors
// connection-status.ts).
//
// The list is reassigned (not mutated) on every push so Svelte's reactivity
// reliably invalidates on each event — including live shell_output chunks,
// which mutate an existing entry's stdout/stderr buffer in place.

import { Timeline, type TimelineEntry } from './timeline';
import type { DaemonEventUnion } from './protocol';

const timeline = new Timeline();

export let entries = $state<TimelineEntry[]>([]);

/** Append one validated daemon event to the timeline. */
export function push(event: DaemonEventUnion): void {
  timeline.push(event);
  entries = [...timeline.entries];
}

/** Drop all timeline entries. */
export function clear(): void {
  timeline.clear();
  entries = [];
}
