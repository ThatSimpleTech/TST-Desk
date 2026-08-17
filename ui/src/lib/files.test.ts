// Per-file write fold tests (TD-1705).
//
// Entries are built by pushing real daemon events through the real Timeline,
// so the fold is exercised against what the store actually holds rather than
// against hand-shaped entries. The diff strings are verbatim output of the
// daemon's own renderer (core/tstd/tools/diff.py) — including the `a//abs`
// header the absolute label produces — so a change in the emitted shape
// fails here instead of silently emptying the pane.

import { describe, it, expect } from "vitest";
import { Timeline, type TimelineEntry } from "./timeline";
import type { DaemonEventUnion } from "./protocol";
import { baseName, dirName, foldFileWrites, parseDiffSections } from "./files";

const EDIT_DIFF =
	"--- a//ws/src/app.py\n" +
	"+++ b//ws/src/app.py\n" +
	"@@ -1,3 +1,4 @@\n" +
	" one\n" +
	"-two\n" +
	"+two point five\n" +
	" three\n" +
	"+four";

const CREATE_DIFF =
	"--- a//ws/src/new.py\n" +
	"+++ b//ws/src/new.py\n" +
	"@@ -0,0 +1,2 @@\n" +
	"+new file\n" +
	"+line two";

const DELETE_DIFF = "--- a//ws/old.txt\n+++ b//ws/old.txt\n@@ -1 +0,0 @@\n-gone";

let seq = 0;

function call(toolCallId: string, name: string, args: Record<string, unknown>): DaemonEventUnion {
	return {
		type: "tool_call",
		seq: ++seq,
		session_id: "s1",
		tool_call_id: toolCallId,
		name,
		arguments: args,
	};
}

function result(toolCallId: string, diff: string | null): DaemonEventUnion {
	return {
		type: "tool_result",
		seq: ++seq,
		session_id: "s1",
		tool_call_id: toolCallId,
		status: "success",
		output: "ok",
		truncated: false,
		diff,
	};
}

/** The entries a session of *events* leaves in the store. */
function entriesFor(events: DaemonEventUnion[]): readonly TimelineEntry[] {
	seq = 0;
	const timeline = new Timeline();
	// The store shows one session (TD-1009); these fixtures are all "s1".
	timeline.bind("s1");
	timeline.pushAll(events);
	return timeline.entries;
}

describe("parseDiffSections", () => {
	it("takes the path from the +++ header, absolute label and all", () => {
		const [section] = parseDiffSections(EDIT_DIFF);
		expect(section.path).toBe("/ws/src/app.py");
	});

	it("counts added and removed lines, not the headers", () => {
		const [section] = parseDiffSections(EDIT_DIFF);
		expect(section.added).toBe(2);
		expect(section.removed).toBe(1);
	});

	it("keeps the section verbatim so it can be rendered as-is", () => {
		const [section] = parseDiffSections(EDIT_DIFF);
		expect(section.diff).toBe(EDIT_DIFF);
	});

	it("reads a created file as all additions", () => {
		const [section] = parseDiffSections(CREATE_DIFF);
		expect(section).toMatchObject({ path: "/ws/src/new.py", added: 2, removed: 0 });
	});

	it("reads a deleted file as all removals", () => {
		const [section] = parseDiffSections(DELETE_DIFF);
		expect(section).toMatchObject({ path: "/ws/old.txt", added: 0, removed: 1 });
	});

	it("splits the two sections a two-target write joins with a blank line", () => {
		const sections = parseDiffSections(`${EDIT_DIFF}\n\n${CREATE_DIFF}`);
		expect(sections.map((s) => s.path)).toEqual(["/ws/src/app.py", "/ws/src/new.py"]);
		// The blank separator belongs to neither section.
		expect(sections[0].diff).toBe(EDIT_DIFF);
		expect(sections[1].diff).toBe(CREATE_DIFF);
	});

	it("does not mistake a removed line that looks like a header for one", () => {
		// A source line of `-- a/x` removed renders as `--- a/x`, and the line
		// after it could be an addition starting `+++`. Mid-hunk, that is
		// content — only a pair at the start of a section opens a new file.
		const diff =
			"--- a//ws/doc.md\n" +
			"+++ b//ws/doc.md\n" +
			"@@ -1,2 +1,2 @@\n" +
			"--- a/not-a-file\n" +
			"+++ b/not-a-file\n";
		const sections = parseDiffSections(diff);
		expect(sections).toHaveLength(1);
		expect(sections[0].path).toBe("/ws/doc.md");
	});

	it("returns nothing for text with no header at all", () => {
		expect(parseDiffSections("just some output")).toEqual([]);
	});
});

describe("foldFileWrites", () => {
	it("is empty before anything is written", () => {
		const summary = foldFileWrites(entriesFor([]));
		expect(summary).toMatchObject({ files: [], fileCount: 0, writeCount: 0, added: 0, removed: 0 });
	});

	it("ignores results that carry no diff, so reads never list a file", () => {
		const summary = foldFileWrites(
			entriesFor([call("t1", "fs_read", { path: "/ws/src/app.py" }), result("t1", null)]),
		);
		expect(summary.files).toEqual([]);
	});

	it("lists a written file with its path and counts", () => {
		const summary = foldFileWrites(
			entriesFor([call("t1", "fs_write", { path: "src/app.py" }), result("t1", EDIT_DIFF)]),
		);
		expect(summary.fileCount).toBe(1);
		expect(summary.files[0]).toMatchObject({ path: "/ws/src/app.py", added: 2, removed: 1 });
	});

	it("names the tool from the call the result answers", () => {
		const summary = foldFileWrites(
			entriesFor([call("t1", "fs_write", { path: "src/app.py" }), result("t1", EDIT_DIFF)]),
		);
		expect(summary.files[0].writes[0].tool).toBe("fs_write");
	});

	it("leaves the tool null when the call was never seen", () => {
		const summary = foldFileWrites(entriesFor([result("t9", EDIT_DIFF)]));
		expect(summary.files[0].writes[0].tool).toBeNull();
	});

	it("folds repeated writes to one path into one file, oldest write first", () => {
		const summary = foldFileWrites(
			entriesFor([
				call("t1", "fs_write", { path: "src/app.py" }),
				result("t1", EDIT_DIFF),
				call("t2", "fs_edit", { path: "src/app.py" }),
				result("t2", EDIT_DIFF),
			]),
		);
		expect(summary.fileCount).toBe(1);
		expect(summary.writeCount).toBe(2);
		expect(summary.files[0].added).toBe(4);
		expect(summary.files[0].removed).toBe(2);
		expect(summary.files[0].writes.map((w) => w.tool)).toEqual(["fs_write", "fs_edit"]);
	});

	it("counts every section of a two-target write as its own file", () => {
		const summary = foldFileWrites(
			entriesFor([
				call("t1", "fs_move", { path: "src/app.py", dest: "src/new.py" }),
				result("t1", `${EDIT_DIFF}\n\n${CREATE_DIFF}`),
			]),
		);
		expect(summary.fileCount).toBe(2);
		expect(summary.writeCount).toBe(2);
	});

	it("puts the most recently written file first", () => {
		const summary = foldFileWrites(
			entriesFor([
				call("t1", "fs_write", { path: "src/app.py" }),
				result("t1", EDIT_DIFF),
				call("t2", "fs_write", { path: "src/new.py" }),
				result("t2", CREATE_DIFF),
			]),
		);
		expect(summary.files.map((f) => f.path)).toEqual(["/ws/src/new.py", "/ws/src/app.py"]);
	});

	it("re-sorts a file to the front when it is written again", () => {
		const summary = foldFileWrites(
			entriesFor([
				call("t1", "fs_write", { path: "src/app.py" }),
				result("t1", EDIT_DIFF),
				call("t2", "fs_write", { path: "src/new.py" }),
				result("t2", CREATE_DIFF),
				call("t3", "fs_edit", { path: "src/app.py" }),
				result("t3", EDIT_DIFF),
			]),
		);
		expect(summary.files.map((f) => f.path)).toEqual(["/ws/src/app.py", "/ws/src/new.py"]);
	});

	it("totals lines across every file", () => {
		const summary = foldFileWrites(
			entriesFor([
				call("t1", "fs_write", { path: "src/app.py" }),
				result("t1", EDIT_DIFF),
				call("t2", "fs_write", { path: "src/new.py" }),
				result("t2", CREATE_DIFF),
				call("t3", "fs_delete", { path: "old.txt" }),
				result("t3", DELETE_DIFF),
			]),
		);
		expect(summary).toMatchObject({ fileCount: 3, writeCount: 3, added: 4, removed: 2 });
	});

	it("keeps each write's own diff for the per-file view", () => {
		const summary = foldFileWrites(
			entriesFor([
				call("t1", "fs_write", { path: "src/app.py" }),
				result("t1", CREATE_DIFF),
				call("t2", "fs_move", { path: "src/app.py" }),
				result("t2", `${CREATE_DIFF}\n\n${EDIT_DIFF}`),
			]),
		);
		const created = summary.files.find((f) => f.path === "/ws/src/new.py");
		expect(created?.writes.map((w) => w.diff)).toEqual([CREATE_DIFF, CREATE_DIFF]);
	});
});

describe("path display", () => {
	it("splits a path into its name and its directory", () => {
		expect(baseName("/ws/src/app.py")).toBe("app.py");
		expect(dirName("/ws/src/app.py")).toBe("/ws/src");
	});

	it("handles a bare name with no directory", () => {
		expect(baseName("app.py")).toBe("app.py");
		expect(dirName("app.py")).toBe("");
	});
});
