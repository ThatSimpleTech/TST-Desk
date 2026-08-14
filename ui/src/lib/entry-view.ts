// Presentation mapping for timeline entries (AC #6).
//
// Maps an entry's kind to a semantic tone + human label, and classifies
// unified-diff lines for the syntax-highlighted file-write view (AC #3).
// Pure and DOM-free so the component stays presentational and this logic
// unit-tests under vitest's node environment.

import type { EntryKind, TimelineEntry } from './timeline';

export type EntryTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

export const KIND_LABELS: Record<EntryKind, string> = {
  tool_call: 'Tool',
  tool_result: 'Result',
  decision: 'Decision',
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
      return entry.details.status === 'error' ? 'danger' : 'success';
    case 'compaction':
      return 'warning';
    case 'decision':
    case 'tier_switch':
    case 'steering_reload':
      return 'info';
    case 'tool_call':
    default:
      return 'neutral';
  }
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
