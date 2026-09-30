// @vitest-environment jsdom
//
// TD-3811: the History disclosure asks for the log and opens a run's
// session through the rail's attach. selectRow is the existing path;
// this file spies on it so the chat store does not have to come along.

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

import ScheduledHistory from "./components/ScheduledHistory.svelte";
import { projects, resetProjects, showScheduled } from "./projects.svelte.js";
import { jobRunLabel, sessionMissingCopy } from "./scheduled";
import { resetScheduled, startScheduled } from "./scheduled.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function job(): JobEntry {
	return {
		id: "j1",
		workspace: "/ws/proj",
		instruction: "summarize the inbox",
		cadence: "every 1 hour",
		next_run: null,
		deliver_to: "window",
		paused: false,
		running: false,
		last_run: "2026-08-21T15:00:00+00:00",
		last_status: "ok",
		last_summary: "digest",
		last_session_id: "sess-1",
	};
}

function run(over: Partial<JobRunEntry> = {}): JobRunEntry {
	return {
		started_at: "2026-08-21T15:00:00+00:00",
		scheduled_for: "2026-08-21T12:00:00+00:00",
		trigger: "schedule",
		status: "ok",
		summary: "from the tick",
		session_id: null,
		...over,
	};
}

function button(name: string): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].find(
		(el) => el.textContent?.trim() === name,
	);
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

describe("scheduled history disclosure (TD-3811)", () => {
	it("renders runs and opens a session that is still on the rail", async () => {
		emit({ type: "job_list", seq: 1, jobs: [job()] });
		app = mount(ScheduledHistory, { target: document.body, props: { jobId: "j1" } });
		await tick();
		expect(mocks.sent.filter((msg) => msg.type === "list_job_runs")).toEqual([]);
		expect(button("History").getAttribute("aria-expanded")).toBe("false");

		button("History").click();
		await tick();
		expect(button("History").getAttribute("aria-expanded")).toBe("true");
		expect(mocks.sent.at(-1)).toEqual({ type: "list_job_runs", job_id: "j1" });
		expect(document.body.textContent).toContain("Loading…");

		const manual = run({
			started_at: "2026-08-21T16:00:00+00:00",
			scheduled_for: null,
			trigger: "manual",
			status: "failed",
			summary: "done by hand",
			session_id: "sess-1",
		});
		const scheduledRun = run();
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: [manual, scheduledRun] });
		await tick();

		const text = document.body.textContent ?? "";
		expect(text).toContain(jobRunLabel(manual));
		expect(text).toContain(jobRunLabel(scheduledRun));
		expect(text).toContain("done by hand");
		expect(text).toContain("from the tick");
		expect(text.split("manual").length - 1).toBe(1);
		expect(document.querySelector(".failed")?.textContent).toContain("Failed");
		expect(
			[...document.body.querySelectorAll("button")].filter(
				(el) => el.textContent?.trim() === "Open session",
			),
		).toHaveLength(1);

		mocks.sessions.rows = [{ sessionId: "sess-1" }];
		button("Open session").click();
		await tick();
		expect(mocks.selectRow).toHaveBeenCalledTimes(1);
		expect(mocks.selectRow).toHaveBeenCalledWith("sess-1");
		expect(projects.surface).toBe("home");
	});

	it("says when the session is no longer on the rail", async () => {
		emit({ type: "job_list", seq: 1, jobs: [job()] });
		app = mount(ScheduledHistory, { target: document.body, props: { jobId: "j1" } });
		await tick();
		button("History").click();
		await tick();
		emit({
			type: "job_runs",
			seq: 1,
			job_id: "j1",
			runs: [run({ session_id: "sess-gone", summary: null })],
		});
		await tick();
		expect(document.body.textContent).not.toContain("null");
		button("Open session").click();
		await tick();
		expect(mocks.selectRow).not.toHaveBeenCalled();
		expect(document.body.textContent).toContain(sessionMissingCopy());
		expect(projects.surface).toBe("scheduled");
		expect(
			[...document.body.querySelectorAll("button")].some(
				(el) => el.textContent?.trim() === "Open session",
			),
		).toBe(false);
	});

	it("shows an empty log, then replaces it when the receipt moves", async () => {
		emit({ type: "job_list", seq: 1, jobs: [job()] });
		app = mount(ScheduledHistory, { target: document.body, props: { jobId: "j1" } });
		await tick();
		button("History").click();
		await tick();
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: [] });
		await tick();
		expect(document.body.textContent).toContain("No runs yet.");

		mocks.sent.length = 0;
		emit({
			type: "job_list",
			seq: 1,
			jobs: [{ ...job(), last_run: "2026-08-21T18:00:00+00:00" }],
		});
		await tick();
		expect(mocks.sent).toEqual([{ type: "list_job_runs", job_id: "j1" }]);
		// The previous reply stays up until the new log arrives.
		expect(document.body.textContent).toContain("No runs yet.");

		const next = run({ summary: "after the tick", session_id: null });
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: [next] });
		await tick();
		expect(document.body.textContent).toContain(jobRunLabel(next));
		expect(document.body.textContent).toContain("after the tick");
		expect(document.body.textContent).not.toContain("No runs yet.");
	});
});
