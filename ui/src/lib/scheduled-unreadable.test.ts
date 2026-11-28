// @vitest-environment jsdom
//
// ARTSCHED-32: jobs this version cannot read are kept in jobs.json by the
// daemon; the Scheduled pane says how many, instead of looking empty.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: () => true,
}));

vi.mock("./sessions.svelte.js", () => ({
	selectRow: () => {},
	findOrFetchSession: async () => null,
	sessions: { rows: [] },
}));

import ScheduledPane from "./components/ScheduledPane.svelte";
import { resetScheduled, startScheduled } from "./scheduled.svelte.js";

let app: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	resetScheduled();
	startScheduled();
	app = mount(ScheduledPane, { target: document.body });
});

afterEach(() => {
	if (app) unmount(app);
	app = null;
	document.body.innerHTML = "";
});

describe("unreadable scheduled jobs", () => {
	it("are named, not hidden", async () => {
		mocks.handler?.({ type: "job_list", seq: 1, jobs: [], unreadable: 2 } as DaemonEventUnion);
		await tick();
		const line = document.querySelector(".unreadable")?.textContent ?? "";
		expect(line).toContain("2 scheduled jobs");
		expect(line).toContain("could not be read by this version");
	});

	it("say nothing when every job reads", async () => {
		mocks.handler?.({ type: "job_list", seq: 1, jobs: [] } as DaemonEventUnion);
		await tick();
		expect(document.querySelector(".unreadable")).toBeNull();
	});
});
