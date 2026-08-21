// Work-pane write stack (TD-3203).
//
// Reuses the Files fold's fixtures: same daemon-shaped diffs, same Timeline
// path. The claim under test is that Work is a stack of writes (newest first),
// not the Files path-fold.

import { describe, it, expect } from "vitest";
import { Timeline, type TimelineEntry } from "./timeline";
import type { DaemonEventUnion } from "./protocol";
import { foldFileWrites } from "./files";
import { stackSessionWrites, WORK_EMPTY_COPY } from "./work";

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

function entriesFor(events: DaemonEventUnion[]): readonly TimelineEntry[] {
	seq = 0;
	const timeline = new Timeline();
	timeline.bind("s1");
	timeline.pushAll(events);
	return timeline.entries;
}

describe("stackSessionWrites", () => {
	it("is empty before anything is written", () => {
		const stack = stackSessionWrites(entriesFor([]));
		expect(stack).toMatchObject({ writes: [], writeCount: 0, added: 0, removed: 0 });
	});

	it("lists each write as its own row, even when they share a path", () => {
		const entries = entriesFor([
			call("t1", "fs_write", { path: "src/app.py" }),
			result("t1", EDIT_DIFF),
			call("t2", "fs_edit", { path: "src/app.py" }),
			result("t2", EDIT_DIFF),
		]);
		expect(foldFileWrites(entries).fileCount).toBe(1);
		const stack = stackSessionWrites(entries);
		expect(stack.writeCount).toBe(2);
		expect(stack.writes).toHaveLength(2);
		expect(stack.writes.map((w) => w.path)).toEqual(["/ws/src/app.py", "/ws/src/app.py"]);
	});

	it("puts the newest write first", () => {
		const stack = stackSessionWrites(
			entriesFor([
				call("t1", "fs_write", { path: "src/app.py" }),
				result("t1", EDIT_DIFF),
				call("t2", "fs_write", { path: "src/new.py" }),
				result("t2", CREATE_DIFF),
			]),
		);
		expect(stack.writes.map((w) => w.path)).toEqual(["/ws/src/new.py", "/ws/src/app.py"]);
		expect(stack.writes[0].seq).toBeGreaterThan(stack.writes[1].seq);
	});

	it("carries path, line counts, and the diff for expand", () => {
		const stack = stackSessionWrites(
			entriesFor([call("t1", "fs_write", { path: "src/app.py" }), result("t1", EDIT_DIFF)]),
		);
		expect(stack.writes[0]).toMatchObject({
			path: "/ws/src/app.py",
			added: 2,
			removed: 1,
			diff: EDIT_DIFF,
			tool: "fs_write",
		});
	});

	it("splits a two-target write into two stack rows", () => {
		const stack = stackSessionWrites(
			entriesFor([
				call("t1", "fs_move", { path: "src/app.py", dest: "src/new.py" }),
				result("t1", `${EDIT_DIFF}\n\n${CREATE_DIFF}`),
			]),
		);
		expect(stack.writes.map((w) => w.path).sort()).toEqual(["/ws/src/app.py", "/ws/src/new.py"]);
		expect(new Set(stack.writes.map((w) => w.id)).size).toBe(2);
	});

	it("hands the canonical path a click would open", () => {
		const stack = stackSessionWrites(
			entriesFor([call("t1", "fs_write", { path: "src/app.py" }), result("t1", EDIT_DIFF)]),
		);
		expect(stack.writes[0].path).toBe("/ws/src/app.py");
	});
});

describe("empty copy", () => {
	it("explains the stack fills as the agent writes, and is not the Files sentence", () => {
		expect(WORK_EMPTY_COPY).toMatch(/fills as the agent writes/i);
		expect(WORK_EMPTY_COPY).not.toMatch(/No files written yet/);
	});
});
