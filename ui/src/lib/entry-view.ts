// Presentation mapping for timeline entries (AC #6).
//
// Maps an entry's kind to a semantic tone + human label, and classifies
// unified-diff lines for the syntax-highlighted file-write view (AC #3).
// Pure and DOM-free so the component stays presentational and this logic
// unit-tests under vitest's node environment.

import { formatUsd } from './cost-format';
import { formatDuration } from './duration';
import type { IconName } from './icons';
import type { EntryKind, TimelineEntry } from './timeline';

export type EntryTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

/** Glyph per entry: what kind of thing happened, readable before the words.
 *  Results split on outcome the same way `entryTone` does, so the icon and
 *  the colour can never disagree. */
export function entryIcon(entry: TimelineEntry): IconName {
  switch (entry.kind) {
    case 'turn':
      return 'message-square';
    case 'tool_call':
      return 'terminal';
    case 'tool_result': {
      const tone = entryTone(entry);
      return tone === 'danger' ? 'x' : tone === 'warning' ? 'minus' : 'check';
    }
    case 'decision':
      return 'scroll';
    case 'tier_switch':
      return 'layers';
    case 'compaction':
      return 'archive';
    case 'steering_reload':
      return 'retry';
    case 'approval':
    case 'error':
    default:
      return 'alert';
  }
}

/** True when a detail value reads inline; objects and arrays get a code block. */
export function isScalarDetail(value: unknown): value is string | number | boolean | null {
  return value === null || (typeof value !== 'object' && typeof value !== 'function');
}

export const KIND_LABELS: Record<EntryKind, string> = {
  turn: 'Turn',
  tool_call: 'Tool',
  tool_result: 'Result',
  decision: 'Decision',
  approval: 'Approval',
  tier_switch: 'Tier switch',
  compaction: 'Compaction',
  steering_reload: 'Steering',
  error: 'Error'
};

/** Semantic tone per entry, driving the colored marker (AC #6). */
export function entryTone(entry: TimelineEntry): EntryTone {
  switch (entry.kind) {
    case 'error':
      return 'danger';
    case 'tool_result':
      // A denial is a user choice, not a failure — amber, not red (TD-1007).
      if (entry.details.error_code === 'approval_denied') return 'warning';
      return entry.details.status === 'error' ? 'danger' : 'success';
    case 'compaction':
    case 'approval':
      return 'warning';
    case 'decision':
    case 'tier_switch':
    case 'steering_reload':
      return 'info';
    case 'turn':
      // A header wears its turn's outcome: stopped is a warning, failed is
      // danger, and a running or finished turn stays quiet.
      if (entry.details.status === 'failed') return 'danger';
      if (entry.details.status === 'cancelled' || entry.details.status === 'interrupted') {
        return 'warning';
      }
      return 'neutral';
    case 'tool_call':
    default:
      return 'neutral';
  }
}

/** The right-hand readout of a turn header: what the daemon measured once
 *  the turn closed, or the one word that says why there is no figure yet.
 *  Duration and cost wear the same wording as the chat's "Worked for" line
 *  and the cost meter, so one turn reads the same in all three places. */
export function turnMetrics(entry: TimelineEntry): string {
  const { status, duration, cost } = entry.details;
  if (status === 'running') return 'running…';
  if (status === 'cancelled') return 'cancelled';
  if (status === 'interrupted') return 'interrupted';
  const parts: string[] = [];
  if (typeof duration === 'number') parts.push(formatDuration(duration));
  if (typeof cost === 'number') parts.push(formatUsd(cost));
  if (status === 'failed') parts.push('failed');
  return parts.join(' · ');
}

export type DiffLineKind = 'add' | 'del' | 'hunk' | 'meta' | 'context';

/** Classify one line of a unified diff for syntax highlighting (AC #3). */
export function classifyDiffLine(line: string): DiffLineKind {
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'context';
}

/** Split a diff string into lines, dropping a single trailing empty line. */
export function diffLines(diff: string): string[] {
  const lines = diff.split('\n');
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}
