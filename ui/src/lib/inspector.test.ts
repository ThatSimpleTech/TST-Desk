// Inspector tab-count tests.

import { describe, it, expect } from "vitest";
import { inspectorCounts, tabCount } from "./inspector";
import type { TimelineEntry } from "./timeline";

function result(seq: number, diff: string | null): TimelineEntry {
	return {
		id: `tool_result:${seq}`,
		kind: "tool_result",
		seq,
		toolCallId: `tc${seq}`,
		title: "Result",
		preview: "",
		details: { status: "success", diff },
	};
}

function section(path: string): string {
	return `--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-old\n+new`;
}

describe("inspectorCounts", () => {
	it("is zero for an empty timeline", () => {
		expect(inspectorCounts([])).toEqual({ files: 0, work: 0 });
	});

	it("counts distinct paths for Files and every write for Work", () => {
		const entries = [
			result(2, section("a.txt")),
			result(4, section("a.txt")),
			result(6, section("b.txt")),
		];
		expect(inspectorCounts(entries)).toEqual({ files: 2, work: 3 });
	});

	it("counts each section of a two-target write", () => {
		const entries = [result(2, `${section("a.txt")}\n\n${section("b.txt")}`)];
		expect(inspectorCounts(entries)).toEqual({ files: 2, work: 2 });
	});

	it("ignores results without a diff and non-result rows", () => {
		const turn: TimelineEntry = {
			id: "turn:1",
			kind: "turn",
			seq: 1,
			title: "Turn 1",
			preview: "hi",
			details: { status: "running" },
		};
		expect(inspectorCounts([turn, result(2, null), result(3, "")])).toEqual({ files: 0, work: 0 });
	});
});

describe("tabCount", () => {
	it("hides zero", () => {
		expect(tabCount(0)).toBeNull();
		expect(tabCount(-1)).toBeNull();
	});

	it("shows the count and saturates at 99+", () => {
		expect(tabCount(1)).toBe("1");
		expect(tabCount(99)).toBe("99");
		expect(tabCount(100)).toBe("99+");
	});
});
