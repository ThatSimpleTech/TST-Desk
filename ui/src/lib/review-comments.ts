// Line comments on the Files pane's diffs (TD-5132). Pure and DOM-free.
//
// The person comments on lines of a file's latest write, then sends them all
// as one `submit_line_comments`. The daemon reads the code around each one
// for the model and echoes the comments on the `user_turn` that carries
// them. From there a comment is open until a write after that turn touches
// its lines, then resolved.
//
// Line numbers count the file after the write the diff shows: the `+` side
// of each `@@ -a,b +c,d @@` hunk. Resolution reads only what the daemon
// sent — the turn's anchors and the later writes' own diffs — never the file
// (§6: the UI never derives truth it wasn't given).

import { classifyDiffLine, diffLines, type DiffLineKind } from "./entry-view";
import { parseDiffSections } from "./files";
import type { LineComment, LineCommentAnchor } from "./protocol";
import type { TimelineEntry } from "./timeline";

/** One diff line with its number in the file after the write, if it has one. */
export interface NumberedRow {
  /** The diff line verbatim, prefix included. */
  line: string;
  kind: DiffLineKind;
  /** Line in the new file: set for context and added lines, null otherwise. */
  newLine: number | null;
  /** The line without its one-character diff prefix. */
  text: string;
}

/** A run of removed and added lines in one write, in the numbering of the
 *  file before it. `at` is the first removed line, or for a pure insertion
 *  the line the new ones went in before. */
export interface ChangeBlock {
  at: number;
  removed: number;
  added: number;
}

/** A comment the daemon echoed on a `user_turn`, and what happened to it. */
export interface SentComment extends LineCommentAnchor {
  /** Seq of the `user_turn` that carried it. */
  turnSeq: number;
  status: "open" | "resolved";
  /** Seq of the write that resolved it. */
  resolvedSeq: number | null;
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Most lines one comment may span; the daemon's own limit. */
export const MAX_COMMENT_LINES = 200;

/** Number a single file's diff section by the file after the write. */
export function numberDiffRows(diff: string): NumberedRow[] {
  const rows: NumberedRow[] = [];
  let next: number | null = null;
  for (const line of diffLines(diff)) {
    const kind = classifyDiffLine(line);
    if (kind === "hunk") {
      const m = HUNK.exec(line);
      next = m === null ? null : Number(m[2]);
      rows.push({ line, kind, newLine: null, text: line });
      continue;
    }
    if (kind === "meta" || next === null || line.startsWith("\\")) {
      rows.push({ line, kind, newLine: null, text: line });
      continue;
    }
    if (kind === "del") {
      rows.push({ line, kind, newLine: null, text: line.slice(1) });
      continue;
    }
    rows.push({ line, kind, newLine: next, text: line.slice(1) });
    next += 1;
  }
  return rows;
}

/** The change runs of one file's diff section, oldest line first. */
export function changeBlocks(diff: string): ChangeBlock[] {
  const blocks: ChangeBlock[] = [];
  let old: number | null = null;
  let open: ChangeBlock | null = null;
  const close = () => {
    if (open !== null) blocks.push(open);
    open = null;
  };
  for (const line of diffLines(diff)) {
    const kind = classifyDiffLine(line);
    if (kind === "hunk") {
      close();
      const m = HUNK.exec(line);
      old = m === null ? null : Number(m[1]);
      // `-0,0` names an empty file: its first line is 1.
      if (old === 0) old = 1;
      continue;
    }
    if (kind === "meta" || old === null || line.startsWith("\\")) continue;
    if (kind === "context") {
      close();
      old += 1;
      continue;
    }
    if (open === null) open = { at: old, removed: 0, added: 0 };
    if (kind === "del") {
      open.removed += 1;
      old += 1;
    } else {
      open.added += 1;
    }
  }
  close();
  return blocks;
}

/** Carry a line range through one write. Touched when the write removed one
 *  of its lines or put new ones in it or right beside it; otherwise the
 *  range as numbered after the write. */
export function carryRange(
  start: number,
  end: number,
  blocks: readonly ChangeBlock[],
): { touched: true } | { touched: false; start: number; end: number } {
  let shift = 0;
  for (const b of blocks) {
    if (b.removed > 0) {
      const last = b.at + b.removed - 1;
      if (b.at <= end && last >= start) return { touched: true };
      if (last < start) shift += b.added - b.removed;
    } else {
      if (b.at >= start && b.at <= end + 1) return { touched: true };
      if (b.at < start) shift += b.added;
    }
  }
  return { touched: false, start: start + shift, end: end + shift };
}

/** The commented lines' text, or null when the range is not one unbroken
 *  run of numbered lines in this diff (it crosses a gap between hunks). */
export function rangeLines(
  rows: readonly NumberedRow[],
  start: number,
  end: number,
): string[] | null {
  const byLine = new Map<number, string>();
  for (const r of rows) if (r.newLine !== null) byLine.set(r.newLine, r.text);
  const out: string[] = [];
  for (let n = start; n <= end; n++) {
    const text = byLine.get(n);
    if (text === undefined) return null;
    out.push(text);
  }
  return out;
}

/** "line 4" or "lines 4-7". */
export function spanLabel(start: number, end: number): string {
  return start === end ? `line ${start}` : `lines ${start}-${end}`;
}

/** The anchors a turn entry carried, if any. */
function turnAnchors(entry: TimelineEntry): LineCommentAnchor[] {
  if (entry.kind !== "turn") return [];
  const raw = entry.details.line_comments;
  return Array.isArray(raw) ? (raw as LineCommentAnchor[]) : [];
}

/** Every comment the daemon echoed this session, with its status. A write
 *  after the comment's turn that touches its lines resolves it; writes that
 *  only move it carry its range along. */
export function sentComments(entries: readonly TimelineEntry[]): SentComment[] {
  const out: SentComment[] = [];
  const live: { comment: SentComment; start: number; end: number }[] = [];
  for (const entry of entries) {
    for (const a of turnAnchors(entry)) {
      const comment: SentComment = {
        id: a.id,
        path: a.path,
        start_line: a.start_line,
        end_line: a.end_line,
        body: a.body,
        turnSeq: entry.seq,
        status: "open",
        resolvedSeq: null,
      };
      out.push(comment);
      live.push({ comment, start: a.start_line, end: a.end_line });
    }
    if (entry.kind !== "tool_result") continue;
    const diff = entry.details.diff;
    if (typeof diff !== "string" || diff === "") continue;
    for (const section of parseDiffSections(diff)) {
      const blocks = changeBlocks(section.diff);
      for (const item of live) {
        if (
          item.comment.status !== "open" ||
          item.comment.path !== section.path
        )
          continue;
        const carried = carryRange(item.start, item.end, blocks);
        if (carried.touched) {
          item.comment.status = "resolved";
          item.comment.resolvedSeq = entry.seq;
        } else {
          item.start = carried.start;
          item.end = carried.end;
        }
      }
    }
  }
  return out;
}

/** A comment not yet sent. */
export interface DraftComment {
  id: string;
  path: string;
  start_line: number;
  end_line: number;
  body: string;
  lines: string[];
}

/** The drafts as the wire takes them, oldest first. */
export function toWire(drafts: readonly DraftComment[]): LineComment[] {
  return drafts.map((d) => ({
    id: d.id,
    path: d.path,
    start_line: d.start_line,
    end_line: d.end_line,
    body: d.body.trim(),
    lines: [...d.lines],
  }));
}
