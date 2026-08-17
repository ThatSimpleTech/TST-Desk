// Tests for the usage store (TD-1706).
//
// The store touches the daemon only through connection-status's
// sendToDaemon/onEvent, so the tests mock exactly that seam and drive the
// same usage_report / usage_exported events the daemon emits. The
// right-pane store is real, so the title-bar link's effect is observable.

import { describe, it, expect, beforeEach } from "vitest";
import { vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, UsageRollup } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	sendOk: true,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return mocks.sendOk;
	},
}));

import {
	usage,
	startUsage,
	resetUsage,
	refreshUsage,
	openUsage,
	selectBucket,
	exportUsage,
} from "./usage.svelte.js";
import { rightPane, resetRightPane } from "./right-pane.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function rollup(over: Partial<UsageRollup> = {}): UsageRollup {
	return {
		bucket: "day",
		key: "2026-08-17",
		tier: "brain",
		prompt_tokens: 40_000,
		cached_prompt_tokens: 0,
		completion_tokens: 5_000,
		cost: 0.195,
		classifier_cost: 0,
		...over,
	};
}

const report = (rows: UsageRollup[]): DaemonEventUnion =>
	({ type: "usage_report", seq: 1, rows }) as DaemonEventUnion;

beforeEach(() => {
	resetUsage();
	resetRightPane();
	mocks.sent.length = 0;
	mocks.sendOk = true;
});

describe("refresh and reduce", () => {
	it("asks the daemon and marks the request in flight", () => {
		startUsage();
		refreshUsage();
		expect(usage.loading).toBe(true);
		expect(mocks.sent.some((m) => m.type === "get_usage")).toBe(true);
	});

	it("the report settles loading and exposes the rows", () => {
		startUsage();
		refreshUsage();
		emit(report([rollup()]));
		expect(usage.loading).toBe(false);
		expect(usage.loaded).toBe(true);
		expect(usage.rows).toHaveLength(1);
		expect(usage.rows[0].cost).toBeCloseTo(0.195, 10);
	});

	it("an empty report is a real answer, not a pending one", () => {
		startUsage();
		refreshUsage();
		emit(report([]));
		expect(usage.loaded).toBe(true);
		expect(usage.rows).toEqual([]);
	});

	it("a second refresh while in flight does not double-send", () => {
		startUsage();
		refreshUsage();
		refreshUsage();
		expect(mocks.sent.filter((m) => m.type === "get_usage")).toHaveLength(1);
	});

	it("socket down reports honestly instead of spinning forever", () => {
		startUsage();
		mocks.sendOk = false;
		refreshUsage();
		expect(usage.loading).toBe(false);
		expect(usage.error).toContain("No connection");
	});
});

describe("bucket selection", () => {
	it("defaults to day and switches on demand", () => {
		expect(usage.bucket).toBe("day");
		selectBucket("week");
		expect(usage.bucket).toBe("week");
		selectBucket("session");
		expect(usage.bucket).toBe("session");
	});

	it("switching buckets does not re-query — one report carries them all", () => {
		startUsage();
		refreshUsage();
		emit(report([rollup(), rollup({ bucket: "week", key: "2026-08-17" })]));
		mocks.sent.length = 0;
		selectBucket("week");
		expect(mocks.sent).toHaveLength(0);
		expect(usage.rows).toHaveLength(2);
	});
});

describe("the title-bar meter's link", () => {
	it("opens the usage tab and loads it", () => {
		startUsage();
		expect(rightPane.tab).toBe("activity");
		openUsage();
		expect(rightPane.tab).toBe("usage");
		expect(mocks.sent.some((m) => m.type === "get_usage")).toBe(true);
	});
});

describe("export", () => {
	it("sends the chosen format", () => {
		startUsage();
		exportUsage("csv");
		expect(mocks.sent).toContainEqual({ type: "export_usage", format: "csv" });
		expect(usage.exporting).toBe(true);
	});

	it("the ack reports where the file landed", () => {
		startUsage();
		exportUsage("jsonl");
		emit({
			type: "usage_exported",
			seq: 1,
			format: "jsonl",
			path: "/data/exports/usage-20260817T120000Z.jsonl",
			rows: 42,
		} as DaemonEventUnion);
		expect(usage.exporting).toBe(false);
		expect(usage.lastExport).toEqual({
			format: "jsonl",
			path: "/data/exports/usage-20260817T120000Z.jsonl",
			rows: 42,
		});
	});

	it("a stale result is cleared when a new export starts", () => {
		startUsage();
		exportUsage("csv");
		emit({
			type: "usage_exported",
			seq: 1,
			format: "csv",
			path: "/data/exports/a.csv",
			rows: 1,
		} as DaemonEventUnion);
		exportUsage("jsonl");
		expect(usage.lastExport).toBeNull();
	});

	it("a failed export clears the in-flight flag instead of wedging", () => {
		startUsage();
		exportUsage("csv");
		emit({
			type: "error",
			seq: 1,
			code: "export_failed",
			message: "Could not write the usage export: disk full",
		} as DaemonEventUnion);
		expect(usage.exporting).toBe(false);
		expect(usage.error).toContain("disk full");
	});

	it("an unrelated error while idle is not mistaken for an export failure", () => {
		startUsage();
		emit({
			type: "error",
			seq: 1,
			code: "bad_request",
			message: "something else",
		} as DaemonEventUnion);
		expect(usage.error).toBeNull();
	});

	it("a second export while one is in flight does not double-send", () => {
		startUsage();
		exportUsage("csv");
		exportUsage("csv");
		expect(mocks.sent.filter((m) => m.type === "export_usage")).toHaveLength(1);
	});

	it("socket down reports honestly", () => {
		startUsage();
		mocks.sendOk = false;
		exportUsage("csv");
		expect(usage.exporting).toBe(false);
		expect(usage.error).toContain("No connection");
	});
});
