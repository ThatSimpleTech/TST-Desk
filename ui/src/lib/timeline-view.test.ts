// Filter and fold rules for the activity pane (navigation round, September 2026).

import { describe, expect, it } from 'vitest';
import type { TimelineEntry } from './timeline';
import {
  TIMELINE_FILTERS,
  filterCounts,
  foldTimeline,
  matchesFilter,
  turnIdOf,
  turnIds
} from './timeline-view';

function entry(
  kind: TimelineEntry['kind'],
  seq: number,
  details: Record<string, unknown> = {}
): TimelineEntry {
  return { id: `${kind}:${seq}`, kind, seq, title: kind, preview: '', details };
}

const turn = (seq: number, turnId: string) =>
  entry('turn', seq, { turn_id: turnId, status: 'running' });
const ok = (seq: number) => entry('tool_result', seq, { status: 'success', error_code: null });
const failed = (seq: number) => entry('tool_result', seq, { status: 'error', error_code: null });
const denied = (seq: number) =>
  entry('tool_result', seq, { status: 'error', error_code: 'approval_denied' });

// Two turns: the first with a call, a result, an approval and its denial;
// the second with a call, a failed result, a daemon error, a decision and a
// tier switch.
const SESSION: TimelineEntry[] = [
  turn(1, 'a'),
  entry('tool_call', 2),
  ok(3),
  entry('approval', 4),
  denied(5),
  turn(6, 'b'),
  entry('tool_call', 7),
  failed(8),
  entry('error', 9),
  entry('decision', 10),
  entry('tier_switch', 11)
];

const NONE: ReadonlySet<string> = new Set();
const ids = (rows: TimelineEntry[]) => rows.map((r) => r.id);

describe('matchesFilter', () => {
  it('keeps every entry under All', () => {
    for (const e of SESSION) expect(matchesFilter(e, 'all')).toBe(true);
  });

  it('keeps calls and results under Tools, and nothing else', () => {
    expect(SESSION.filter((e) => matchesFilter(e, 'tools') && e.kind !== 'turn').map((e) => e.kind)).toEqual([
      'tool_call',
      'tool_result',
      'tool_result',
      'tool_call',
      'tool_result'
    ]);
  });

  it('keeps requests and the decisions that resolve them under Approvals', () => {
    expect(ids(SESSION.filter((e) => matchesFilter(e, 'approvals') && e.kind !== 'turn'))).toEqual([
      'approval:4',
      'decision:10'
    ]);
  });

  it('keeps failed results and daemon errors under Errors — not a denial', () => {
    expect(ids(SESSION.filter((e) => matchesFilter(e, 'errors') && e.kind !== 'turn'))).toEqual([
      'tool_result:8',
      'error:9'
    ]);
    expect(matchesFilter(denied(1), 'errors')).toBe(false);
    expect(matchesFilter(ok(1), 'errors')).toBe(false);
  });

  it('never filters a header out', () => {
    for (const chip of TIMELINE_FILTERS) expect(matchesFilter(turn(1, 'a'), chip.id)).toBe(true);
  });
});

describe('filterCounts', () => {
  it('counts body rows per chip, headers excluded', () => {
    expect(filterCounts(SESSION)).toEqual({ all: 9, tools: 5, approvals: 2, errors: 2 });
  });

  it('is all zeros for an empty session', () => {
    expect(filterCounts([])).toEqual({ all: 0, tools: 0, approvals: 0, errors: 0 });
  });
});

describe('foldTimeline', () => {
  it('draws everything when nothing is filtered or folded', () => {
    const view = foldTimeline(SESSION, 'all', NONE);
    expect(view.rows).toEqual(SESSION);
    expect(view.turns.get('turn:1')).toEqual({ matched: 4, hidden: 0 });
    expect(view.turns.get('turn:6')).toEqual({ matched: 5, hidden: 0 });
  });

  it('narrows the body under each header and keeps the headers', () => {
    const view = foldTimeline(SESSION, 'errors', NONE);
    expect(ids(view.rows)).toEqual(['turn:1', 'turn:6', 'tool_result:8', 'error:9']);
    // The first turn had no errors — its header still stands, with nothing under it.
    expect(view.turns.get('turn:1')).toEqual({ matched: 0, hidden: 0 });
    expect(view.turns.get('turn:6')).toEqual({ matched: 2, hidden: 0 });
  });

  it('hides a folded turn’s rows and says how many', () => {
    const view = foldTimeline(SESSION, 'all', new Set(['turn:1']));
    expect(ids(view.rows)).toEqual([
      'turn:1',
      'turn:6',
      'tool_call:7',
      'tool_result:8',
      'error:9',
      'decision:10',
      'tier_switch:11'
    ]);
    expect(view.turns.get('turn:1')).toEqual({ matched: 4, hidden: 4 });
  });

  it('folds what the filter left, so the count on the header matches the chip', () => {
    const view = foldTimeline(SESSION, 'tools', new Set(['turn:6']));
    expect(ids(view.rows)).toEqual(['turn:1', 'tool_call:2', 'tool_result:3', 'tool_result:5', 'turn:6']);
    expect(view.turns.get('turn:6')).toEqual({ matched: 2, hidden: 2 });
  });

  it('lets rows before the first header through, unfolded and uncounted', () => {
    const view = foldTimeline([ok(1), turn(2, 'a')], 'all', new Set(['turn:2']));
    expect(ids(view.rows)).toEqual(['tool_result:1', 'turn:2']);
    expect(view.turns.size).toBe(1);
    expect(view.turns.get('turn:2')).toEqual({ matched: 0, hidden: 0 });
  });

  it('ignores a collapsed id that is not a header', () => {
    const view = foldTimeline(SESSION, 'all', new Set(['tool_call:2']));
    expect(view.rows).toEqual(SESSION);
  });
});

describe('turn ids', () => {
  it('reads the daemon’s turn id off a header, and nothing off anything else', () => {
    expect(turnIdOf(turn(1, 'a'))).toBe('a');
    expect(turnIdOf(entry('turn', 1, { turn_id: '' }))).toBeNull();
    expect(turnIdOf(entry('turn', 1, { turn_id: 7 }))).toBeNull();
    expect(turnIdOf(ok(1))).toBeNull();
  });

  it('lists every header in order', () => {
    expect(turnIds(SESSION)).toEqual(['turn:1', 'turn:6']);
    expect(turnIds([])).toEqual([]);
  });
});

describe('TIMELINE_FILTERS', () => {
  it('offers the four chips in reading order, All first', () => {
    expect(TIMELINE_FILTERS.map((c) => c.id)).toEqual(['all', 'tools', 'approvals', 'errors']);
  });

  it('gives every chip a label and hover copy', () => {
    for (const chip of TIMELINE_FILTERS) {
      expect(chip.label.length).toBeGreaterThan(0);
      expect(chip.hint.length).toBeGreaterThan(0);
    }
  });
});
