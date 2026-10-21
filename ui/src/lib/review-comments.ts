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
  /** `outdated`: a write before the comment's turn changed its lines, so
   *  the agent read it against code that had already moved. */
  status: "open" | "resolved" | "outdated";
  /** Seq of the write that resolved or outdated it. */
  resolvedSeq: number | null;
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/** Most lines one comment may span; the daemon's own limit. */
export const MAX_COMMENT_LINES = 200;

/** One diff line with its numbers on both sides of the write. */
interface WalkedRow {
  line: string;
  kind: DiffLineKind;
  /** Line in the file before the write: context and removed lines. */
  oldLine: number | null;
  /** Line in the file after the write: context and added lines. */
  newLine: number | null;
}

/** Walk a diff by its hunk headers' own line counts. Inside a hunk a line is
 *  classed by its first character alone, so an added `++i;` or a removed
 *  `-- note` / `---` rule is a content line, not a `+++`/`---` file header
 *  (which only appear outside hunks). */
/** The marker the daemon's output cap ends a cut tool result with
 *  (`truncate_output` in core/tstd/tools/results.py). */
export const TRUNCATION_MARKER = "\n\n┈─[truncated — results exceed output cap]─┈";

/** A cut diff: the whole lines before the cut, and the marker as one line.
 *  The last line before it may be cut partway, so it goes too (TD-5161):
 *  neither it nor the marker is a line of the file to comment on. */
function splitCut(diff: string): { body: string; cut: string | null } {
  if (!diff.endsWith(TRUNCATION_MARKER)) return { body: diff, cut: null };
  const before = diff.slice(0, -TRUNCATION_MARKER.length);
  const lastBreak = before.lastIndexOf("\n");
  return { body: lastBreak < 0 ? "" : before.slice(0, lastBreak + 1), cut: TRUNCATION_MARKER.trim() };
}

function walkDiff(diff: string): WalkedRow[] {
  const { body, cut } = splitCut(diff);
  const rows = walkWhole(body);
  if (cut !== null) rows.push({ line: cut, kind: "meta", oldLine: null, newLine: null });
  return rows;
}

function walkWhole(diff: string): WalkedRow[] {
  const rows: WalkedRow[] = [];
  let oldNext = 0;
  let newNext = 0;
  let oldLeft = 0;
  let newLeft = 0;
  for (const line of diffLines(diff)) {
    const inHunk = oldLeft > 0 || newLeft > 0;
    if (inHunk) {
      const c = line.charAt(0);
      if (c === "+" && newLeft > 0) {
        rows.push({ line, kind: "add", oldLine: null, newLine: newNext });
        newNext += 1;
        newLeft -= 1;
        continue;
      }
      if (c === "-" && oldLeft > 0) {
        rows.push({ line, kind: "del", oldLine: oldNext, newLine: null });
        oldNext += 1;
        oldLeft -= 1;
        continue;
      }
      // An empty line is a context line whose leading space was trimmed.
      if ((c === " " || line === "") && oldLeft > 0 && newLeft > 0) {
        rows.push({ line, kind: "context", oldLine: oldNext, newLine: newNext });
        oldNext += 1;
        newNext += 1;
        oldLeft -= 1;
        newLeft -= 1;
        continue;
      }
      if (c === "\\") {
        rows.push({ line, kind: "context", oldLine: null, newLine: null });
        continue;
      }
      // Anything else ends a hunk whose counts were wrong: read it afresh.
      oldLeft = 0;
      newLeft = 0;
    }
    const m = line.startsWith("@@") ? HUNK.exec(line) : null;
    if (m !== null) {
      oldNext = Number(m[1]);
      oldLeft = m[2] === undefined ? 1 : Number(m[2]);
      newNext = Number(m[3]);
      newLeft = m[4] === undefined ? 1 : Number(m[4]);
      // `-a,0` removes nothing: its lines go in after line a (`-0,0`, an
      // empty file: before line 1).
      if (oldLeft === 0) oldNext += 1;
      rows.push({ line, kind: "hunk", oldLine: null, newLine: null });
      continue;
    }
    // A `\ No newline` after a hunk's last line, file headers, `diff --git`.
    const kind = line.startsWith("\\") ? "context" : classifyDiffLine(line);
    rows.push({
      line,
      kind: kind === "hunk" ? "hunk" : kind === "context" ? "context" : "meta",
      oldLine: null,
      newLine: null,
    });
  }
  return rows;
}

/** Number a single file's diff section by the file after the write. */
export function numberDiffRows(diff: string): NumberedRow[] {
  return walkDiff(diff).map((r) => ({
    line: r.line,
    kind: r.kind,
    newLine: r.newLine,
    text:
      r.kind === "add" || r.kind === "del" || (r.kind === "context" && r.newLine !== null)
        ? r.line.slice(1)
        : r.line,
  }));
}

/** The change runs of one file's diff section, oldest line first. */
export function changeBlocks(diff: string): ChangeBlock[] {
  const blocks: ChangeBlock[] = [];
  let open: ChangeBlock | null = null;
  // The old-file line the next removal or insertion lands on.
  let old = 1;
  const close = () => {
    if (open !== null) blocks.push(open);
    open = null;
  };
  for (const r of walkDiff(diff)) {
    if (r.kind === "add") {
      if (open === null) open = { at: old, removed: 0, added: 0 };
      open.added += 1;
    } else if (r.kind === "del" && r.oldLine !== null) {
      if (open === null) open = { at: r.oldLine, removed: 0, added: 0 };
      open.removed += 1;
      old = r.oldLine + 1;
    } else if (r.kind === "context" && r.oldLine !== null) {
      close();
      old = r.oldLine + 1;
    } else if (r.kind === "hunk") {
      close();
      const m = HUNK.exec(r.line);
      if (m === null) old = 1;
      else old = m[2] === "0" ? Number(m[1]) + 1 : Number(m[1]);
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

/** Every comment the daemon echoed this session, with its status.
 *
 *  A comment's range counts lines of the write it was made on (`base_seq`).
 *  Writes after that one and before the comment's turn, which a running
 *  turn makes while the comment waits in the queue, carry the range along;
 *  one that touches its lines leaves it `outdated`, since the agent changed
 *  them before it read the comment. After the turn, a write that touches
 *  its lines resolves it. Without `base_seq` (an older window), tracking
 *  starts at the turn. */
export function sentComments(entries: readonly TimelineEntry[]): SentComment[] {
  const out: SentComment[] = [];
  const live: { comment: SentComment; from: number; start: number; end: number }[] = [];
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
      const from = typeof a.base_seq === "number" ? a.base_seq : entry.seq;
      live.push({ comment, from, start: a.start_line, end: a.end_line });
    }
  }
  // Every write with a diff, in log order.
  for (const entry of entries) {
    if (entry.kind !== "tool_result") continue;
    const diff = entry.details.diff;
    if (typeof diff !== "string" || diff === "") continue;
    for (const section of parseDiffSections(diff)) {
      const blocks = changeBlocks(section.diff);
      for (const item of live) {
        if (
          item.comment.status !== "open" ||
          item.comment.path !== section.path ||
          entry.seq <= item.from
        )
          continue;
        const carried = carryRange(item.start, item.end, blocks);
        if (carried.touched) {
          item.comment.status = entry.seq < item.comment.turnSeq ? "outdated" : "resolved";
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
  /** Seq of the write whose diff the comment was made on, if known. */
  base_seq: number | null;
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
    ...(d.base_seq === null ? {} : { base_seq: d.base_seq }),
  }));
}
