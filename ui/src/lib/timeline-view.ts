// View-side folding of the activity timeline (navigation round, September 2026).
//
// The store (timeline.ts) keeps every entry a session produced, in order.
// This is what the pane draws from it: the rows left after a filter chip
// and after the turns the user has folded shut. Pure and DOM-free like
// entry-view.ts, so the rules unit-test in node and ActivityTimeline stays
// a view with one `$derived` in it.
//
// Two rules the tests pin:
//   - a turn header is never filtered out. Headers are the navigation; a
//     filter narrows what sits under them, and a header with nothing under
//     it still says a turn happened.
//   - a collapsed turn hides its body, filter or not, and the header
//     reports how many rows it is sitting on.

import { entryTone } from './entry-view';
import type { TimelineEntry } from './timeline';

export type TimelineFilter = 'all' | 'tools' | 'approvals' | 'errors';

export interface FilterChip {
  id: TimelineFilter;
  label: string;
  /** Hover copy: what the chip keeps. */
  hint: string;
}

export const TIMELINE_FILTERS: readonly FilterChip[] = [
  { id: 'all', label: 'All', hint: 'Every entry' },
  { id: 'tools', label: 'Tools', hint: 'Tool calls and their results' },
  {
    id: 'approvals',
    label: 'Approvals',
    hint: 'Approval requests and the decisions that resolved them'
  },
  { id: 'errors', label: 'Errors', hint: 'Failed results and daemon errors' }
];

/** Whether an entry belongs under `filter`. Headers belong under all of them. */
export function matchesFilter(entry: TimelineEntry, filter: TimelineFilter): boolean {
  if (entry.kind === 'turn') return true;
  switch (filter) {
    case 'all':
      return true;
    case 'tools':
      return entry.kind === 'tool_call' || entry.kind === 'tool_result';
    case 'approvals':
      return entry.kind === 'approval' || entry.kind === 'decision';
    case 'errors':
      // The same rule that paints a row red: an `error` event, or a result
      // whose status is error. A denial is amber, and not an error.
      return entryTone(entry) === 'danger';
  }
}

/** How many body rows each chip would keep — the number on the chip. */
export function filterCounts(entries: readonly TimelineEntry[]): Record<TimelineFilter, number> {
  const counts: Record<TimelineFilter, number> = { all: 0, tools: 0, approvals: 0, errors: 0 };
  for (const entry of entries) {
    if (entry.kind === 'turn') continue;
    for (const chip of TIMELINE_FILTERS) {
      if (matchesFilter(entry, chip.id)) counts[chip.id] += 1;
    }
  }
  return counts;
}

export interface TurnFold {
  /** Body rows under the header that pass the filter. */
  matched: number;
  /** Of those, how many the collapse is hiding (0 while open). */
  hidden: number;
}

export interface TimelineView {
  /** The rows to draw, in order. */
  rows: TimelineEntry[];
  /** Per header id, what sits under it. */
  turns: Map<string, TurnFold>;
}

/**
 * The rows the pane draws: every header, and under each header that is not
 * in `collapsed`, the body rows that pass `filter`. Rows before the first
 * header (an attach window that opened mid-turn) belong to no turn: they
 * follow the filter and cannot be folded.
 */
export function foldTimeline(
  entries: readonly TimelineEntry[],
  filter: TimelineFilter,
  collapsed: ReadonlySet<string>
): TimelineView {
  const rows: TimelineEntry[] = [];
  const turns = new Map<string, TurnFold>();
  let fold: TurnFold | null = null;
  let folded = false;
  for (const entry of entries) {
    if (entry.kind === 'turn') {
      fold = { matched: 0, hidden: 0 };
      folded = collapsed.has(entry.id);
      turns.set(entry.id, fold);
      rows.push(entry);
      continue;
    }
    if (!matchesFilter(entry, filter)) continue;
    if (fold !== null) {
      fold.matched += 1;
      if (folded) {
        fold.hidden += 1;
        continue;
      }
    }
    rows.push(entry);
  }
  return { rows, turns };
}

/** The daemon's id for the turn a header opened — what the chat holds the
 *  same message under — or null when the header carries none. */
export function turnIdOf(entry: TimelineEntry): string | null {
  const id = entry.details.turn_id;
  return typeof id === 'string' && id.length > 0 ? id : null;
}

/** Every header id, in order — the set "collapse all" fills. */
export function turnIds(entries: readonly TimelineEntry[]): string[] {
  const out: string[] = [];
  for (const entry of entries) if (entry.kind === 'turn') out.push(entry.id);
  return out;
}
