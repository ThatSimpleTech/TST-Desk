import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AutonomySummary, ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { ledgerAbsolutePath, openBranchAndLedger } from "./wakeup";

const mocks = vi.hoisted(() => {
	const state = {
		handler: null as ((e: DaemonEventUnion) => void) | null,
		session: { sessionId: "s1" as string | null, workspacePath: "/home/u/proj" as string | null },
	};
	return state;
});

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (_msg: ClientMessageUnion) => true,
}));

vi.mock("./session-status.svelte.js", () => ({
	get session() {
		return mocks.session;
	},
}));

import { bindWakeup, resetWakeup, startWakeup, wakeup } from "./wakeup.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function summary(over: Partial<AutonomySummary> = {}): AutonomySummary {
	return {
		type: "autonomy_summary",
		session_id: "s1",
		seq: 12,
		reason: "definition of done met",
		branch: "tst/auto/ship-the-csv-importer",
		ledger_path: ".tst/autonomy/DECISIONS.md",
		changed: ["src/importer.py"],
		refusals: [],
		ledger_excerpt: "**Chose:** format",
		...over,
	};
}

beforeEach(() => {
	resetWakeup();
	mocks.session.sessionId = "s1";
	mocks.session.workspacePath = "/home/u/proj";
});

afterEach(() => {
	resetWakeup();
});

describe("wakeup store", () => {
	it("shows the card on autonomy_summary for the bound session", () => {
		startWakeup();
		expect(wakeup.summary).toBeNull();
		emit(summary());
		expect(wakeup.summary).not.toBeNull();
		expect(wakeup.summary?.branch).toBe("tst/auto/ship-the-csv-importer");
		expect(wakeup.summary?.ledger_path).toBe(".tst/autonomy/DECISIONS.md");
		expect(wakeup.summary?.changed).toEqual(["src/importer.py"]);
	});

	it("ignores another session and interactive-looking traffic", () => {
		startWakeup();
		emit(summary({ session_id: "other", seq: 1 }));
		expect(wakeup.summary).toBeNull();
		emit({
			type: "decision_logged",
			session_id: "s1",
			seq: 2,
			decision_class: "A",
			what: "format",
			why: "ruff",
			commit: "abc",
		});
		expect(wakeup.summary).toBeNull();
	});

	it("drops the card when the bound session changes", () => {
		startWakeup();
		emit(summary());
		expect(wakeup.summary).not.toBeNull();
		bindWakeup("s2");
		expect(wakeup.summary).toBeNull();
	});
});

describe("open branch and ledger", () => {
	it("fires both opens in one click", async () => {
		const opened: string[] = [];
		const copied: string[] = [];
		const result = await openBranchAndLedger(summary(), "/home/u/proj", {
			openFile: async (path) => {
				opened.push(path);
				return true;
			},
			copyText: async (text) => {
				copied.push(text);
				return true;
			},
		});
		expect(result).toEqual({ opened: true, copied: true });
		expect(opened).toEqual(["/home/u/proj/.tst/autonomy/DECISIONS.md"]);
		expect(copied).toEqual(["tst/auto/ship-the-csv-importer"]);
	});

	it("joins the daemon ledger_path under the workspace", () => {
		expect(ledgerAbsolutePath("/ws", ".tst/autonomy/DECISIONS.md")).toBe(
			"/ws/.tst/autonomy/DECISIONS.md",
		);
		expect(ledgerAbsolutePath(null, ".tst/autonomy/DECISIONS.md")).toBeNull();
	});
});
