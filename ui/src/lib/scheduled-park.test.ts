// @vitest-environment jsdom
//
// TD-3815: a parked approval is "Waiting for approval" on the row and in
// history, with the same Open session attach the history already uses.
// Settling the card does not move last_run, so an open history asks again
// when the status or the summary changes.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, JobRunEntry } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	selectRow: vi.fn<(sessionId: string) => void>(),
	sessions: { rows: [] as { sessionId: string }[] },
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
		return true;
	},
}));

vi.mock("./sessions.svelte.js", () => ({
	selectRow: (sessionId: string) => {
		mocks.selectRow(sessionId);
	},
	sessions: mocks.sessions,
}));

import ScheduledPane from "./components/ScheduledPane.svelte";
import { projects, resetProjects, showScheduled } from "./projects.svelte.js";
import { jobLastRun, jobRunLabel, sessionMissingCopy } from "./scheduled";
import { resetScheduled, scheduled, startScheduled } from "./scheduled.svelte.js";

const STAMP = "2026-08-21T18:00:00+00:00";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function job(over: Partial<JobEntry> = {}): JobEntry {
	return {
		id: "j1",
		workspace: "/ws/proj",
		instruction: "summarize the inbox",
		cadence: "every 1 hour",
		next_run: "2026-08-21T19:00:00+00:00",
		deliver_to: "window",
		paused: false,
		running: false,
		last_run: STAMP,
		last_status: "waiting",
		last_summary: "Run `echo hi`",
		last_session_id: "sess-park",
		...over,
	};
}

function run(over: Partial<JobRunEntry> = {}): JobRunEntry {
	return {
		started_at: STAMP,
		scheduled_for: "2026-08-21T07:45:00+00:00",
		trigger: "schedule",
		status: "waiting",
		summary: "Run `echo hi`",
		session_id: "sess-park",
		...over,
	};
}

function button(name: string, root: ParentNode = document.body): HTMLButtonElement {
	const found = [...root.querySelectorAll("button")].find((el) => el.textContent?.trim() === name);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
}

beforeEach(() => {
	mocks.sent.length = 0;
	mocks.selectRow.mockClear();
	mocks.sessions.rows = [];
	mocks.handler = null;
	resetScheduled();
	resetProjects();
	showScheduled();
	startScheduled();
	mocks.sent.length = 0;
});

afterEach(async () => {
	if (app !== null) {
		await unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	resetScheduled();
	resetProjects();
});

describe("parked approval on the scheduled pane (TD-3815)", () => {
	it("shows Waiting for approval and opens the parked session", async () => {
		const row = job();
		emit({ type: "job_list", seq: 1, jobs: [row] });
		app = mount(ScheduledPane, { target: document.body });
		await tick();

		const card = document.querySelector(".card");
		expect(card).not.toBeNull();
		expect(card?.classList.contains("card-waiting")).toBe(true);
		expect(card?.classList.contains("card-failed")).toBe(false);
		expect(card?.classList.contains("card-missed")).toBe(false);
		expect(card?.querySelector(".run-waiting")?.textContent).toBe(jobLastRun(row));
		expect(card?.textContent).toContain("Waiting for approval");
		expect(card?.textContent).not.toContain("Failed");
		expect(card?.textContent).not.toContain("Missed");

		mocks.sessions.rows = [{ sessionId: "sess-park" }];
		button("Open session", card ?? document.body).click();
		await tick();
		expect(mocks.selectRow).toHaveBeenCalledTimes(1);
		expect(mocks.selectRow).toHaveBeenCalledWith("sess-park");
		expect(projects.surface).toBe("home");
	});

	it("says when the parked session is no longer on the rail", async () => {
		emit({ type: "job_list", seq: 1, jobs: [job()] });
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		const card = document.querySelector(".card");
		button("Open session", card ?? document.body).click();
		await tick();
		expect(mocks.selectRow).not.toHaveBeenCalled();
		expect(card?.textContent).toContain(sessionMissingCopy());
		expect(projects.surface).toBe("scheduled");
	});

	it("shows the same waiting line in history and opens that session", async () => {
		const row = job();
		const parked = run();
		emit({ type: "job_list", seq: 1, jobs: [row] });
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		button("History").click();
		await tick();
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: [parked] });
		await tick();

		const history = document.querySelector(".history");
		expect(history?.querySelector(".waiting")?.textContent).toBe(jobRunLabel(parked));
		expect(history?.textContent).toContain("Waiting for approval");
		expect(history?.textContent).not.toContain("Failed");
		expect(history?.textContent).not.toContain("Missed");

		mocks.sessions.rows = [{ sessionId: "sess-park" }];
		button("Open session", history ?? document.body).click();
		await tick();
		expect(mocks.selectRow).toHaveBeenCalledWith("sess-park");
		expect(projects.surface).toBe("home");
	});

	it("asks again when a parked receipt settles without a new last_run", async () => {
		emit({ type: "job_list", seq: 1, jobs: [job()] });
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		button("History").click();
		await tick();
		expect(scheduled.historySeen.j1).toBe(STAMP);
		mocks.sent.length = 0;

		emit({ type: "job_list", seq: 1, jobs: [job()] });
		await tick();
		expect(mocks.sent).toEqual([]);
		expect(scheduled.historySeen.j1).toBe(STAMP);

		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ last_status: "ok", last_summary: "all done" })],
		});
		await tick();
		expect(mocks.sent).toEqual([{ type: "list_job_runs", job_id: "j1" }]);
		expect(scheduled.historySeen.j1).toBe(STAMP);
	});
});
