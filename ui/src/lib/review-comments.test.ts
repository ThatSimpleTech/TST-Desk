// TD-5132: line comments on the Files pane's diffs.
import { describe, expect, it } from "vitest";
import {
  carryRange,
  changeBlocks,
  numberDiffRows,
  rangeLines,
  sentComments,
  toWire,
} from "./review-comments";
import { turnEntry, type TimelineEntry } from "./timeline";

const PATH = "/w/src/app.py";

/** A diff the daemon renders: headers, then hunks. */
function diff(...hunks: string[]): string {
  return [`--- a/${PATH}`, `+++ b/${PATH}`, ...hunks].join("\n");
}

const FIRST = diff(
  "@@ -1,4 +1,5 @@",
  " a",
  "-b",
  "+B",
  "+B2",
  " c",
  " d",
  "@@ -20,2 +21,2 @@",
  " t",
  "+u",
);

function turn(seq: number, comments: unknown[] | null): TimelineEntry {
  return {
    id: `turn:${seq}`,
    kind: "turn",
    seq,
    title: "Turn",
    preview: "",
    details: { line_comments: comments },
  };
}

function write(seq: number, body: string): TimelineEntry {
  return {
    id: `tool_result:${seq}`,
    kind: "tool_result",
    seq,
    title: "",
    preview: "",
    details: { diff: body },
  };
}

function anchor(id: string, start: number, end: number) {
  return { id, path: PATH, start_line: start, end_line: end, body: "fix" };
}

describe("numberDiffRows", () => {
  it("numbers context and added lines by the file after the write", () => {
    const rows = numberDiffRows(FIRST);
    const numbered = rows.filter((r) => r.newLine !== null).map((r) => [r.newLine, r.text]);
    expect(numbered).toEqual([
      [1, "a"],
      [2, "B"],
      [3, "B2"],
      [4, "c"],
      [5, "d"],
      [21, "t"],
      [22, "u"],
    ]);
    // Removed lines and headers take no comment.
    expect(rows.find((r) => r.line === "-b")?.newLine).toBeNull();
    expect(rows.find((r) => r.kind === "meta")?.newLine).toBeNull();
  });
});

describe("rangeLines", () => {
  it("gives the commented lines, and refuses a range across a hunk gap", () => {
    const rows = numberDiffRows(FIRST);
    expect(rangeLines(rows, 2, 4)).toEqual(["B", "B2", "c"]);
    expect(rangeLines(rows, 5, 21)).toBeNull();
  });
});

describe("changeBlocks / carryRange", () => {
  const later = diff("@@ -3,3 +3,3 @@", " B2", "-c", "+C", " d");

  it("finds the change runs by the file before the write", () => {
    expect(changeBlocks(later)).toEqual([{ at: 4, removed: 1, added: 1 }]);
  });

  it("a write that changes a commented line touches it", () => {
    expect(carryRange(4, 4, changeBlocks(later))).toEqual({ touched: true });
    expect(carryRange(2, 5, changeBlocks(later))).toEqual({ touched: true });
  });

  it("a write elsewhere only moves the comment", () => {
    const above = diff("@@ -1,1 +1,3 @@", " a", "+x", "+y");
    expect(carryRange(10, 12, changeBlocks(above))).toEqual({ touched: false, start: 12, end: 14 });
    const below = diff("@@ -30,1 +30,1 @@", "-z", "+Z");
    expect(carryRange(10, 12, changeBlocks(below))).toEqual({ touched: false, start: 10, end: 12 });
  });

  it("lines put in right beside or inside the range touch it", () => {
    const after = diff("@@ -12,1 +12,2 @@", " l", "+new");
    expect(carryRange(10, 12, changeBlocks(after))).toEqual({ touched: true });
  });
});

describe("sentComments", () => {
  it("a comment is open until a write after its turn touches its lines", () => {
    const touching = diff("@@ -4,1 +4,1 @@", "-c", "+C");
    const open = sentComments([write(1, FIRST), turn(2, [anchor("c1", 4, 4)])]);
    expect(open.map((c) => [c.id, c.status])).toEqual([["c1", "open"]]);

    const done = sentComments([write(1, FIRST), turn(2, [anchor("c1", 4, 4)]), write(3, touching)]);
    expect(done[0].status).toBe("resolved");
    expect(done[0].resolvedSeq).toBe(3);
  });

  it("a write before the turn never resolves it", () => {
    const touching = diff("@@ -4,1 +4,1 @@", "-c", "+C");
    const out = sentComments([write(1, touching), turn(2, [anchor("c1", 4, 4)])]);
    expect(out[0].status).toBe("open");
  });

  it("a write elsewhere carries the range so a later write still finds it", () => {
    const above = diff("@@ -1,1 +1,3 @@", " a", "+x", "+y");
    const moved = diff("@@ -6,1 +6,1 @@", "-c", "+C");
    const out = sentComments([turn(1, [anchor("c1", 4, 4)]), write(2, above), write(3, moved)]);
    expect(out[0].status).toBe("resolved");
  });

  it("a write to another file leaves it open", () => {
    const other = "--- a//w/other.py\n+++ b//w/other.py\n@@ -4,1 +4,1 @@\n-c\n+C";
    const out = sentComments([turn(1, [anchor("c1", 4, 4)]), write(2, other)]);
    expect(out[0].status).toBe("open");
  });

  it("a turn without comments adds none", () => {
    expect(sentComments([turn(1, null), write(2, FIRST)])).toEqual([]);
  });
});

describe("toWire", () => {
  it("sends each draft anchored by file and line, with its lines", () => {
    expect(
      toWire([{ id: "d1", path: PATH, start_line: 2, end_line: 3, body: "  merge  ", lines: ["B", "B2"] }]),
    ).toEqual([{ id: "d1", path: PATH, start_line: 2, end_line: 3, body: "merge", lines: ["B", "B2"] }]);
  });
});

describe("from the user_turn the daemon logs", () => {
  it("the turn entry keeps the comments, so a replay rebuilds them", () => {
    const entry = turnEntry(
      { type: "user_turn", session_id: "s1", turn_id: "t1", content: "Review", seq: 5, line_comments: [anchor("c1", 4, 4)] },
      1,
    );
    expect(sentComments([entry]).map((c) => [c.id, c.path, c.start_line, c.turnSeq])).toEqual([["c1", PATH, 4, 5]]);
  });
});
