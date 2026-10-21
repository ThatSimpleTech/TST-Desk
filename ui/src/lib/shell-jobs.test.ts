// @vitest-environment jsdom
//
// TD-5137: the window lists the session's background shell jobs. The daemon
// owns them; the store keeps the newest `shell_jobs` per session and the Plan
// pane lists the focused one. The socket is mocked so events arrive as the
// daemon's.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { DaemonEventUnion, ShellJobInfo } from "./protocol";
import fixtures from "./protocol-fixtures.json";

const mocks = vi.hoisted(() => ({
	handlers: [] as ((e: DaemonEventUnion) => void)[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handlers.push(handler);
		return () => {
			mocks.handlers = mocks.handlers.filter((h) => h !== handler);
		};
	},
	sendToDaemon: () => true,
}));

import PlanPane from "./components/PlanPane.svelte";
import { resetRightPane } from "./right-pane.svelte.js";
import { ingestEvent, resetSession } from "./session-status.svelte.js";
import {
	jobStateLabel,
	resetShellJobs,
	shellJobsFor,
	startShellJobs,
} from "./shell-jobs-store.svelte.js";
import { resetTaskLists } from "./task-list-store.svelte.js";

const DEV: ShellJobInfo = {
	job_id: "job-1",
	command: "npm run dev",
	status: "running",
	exit_code: null,
	started_at: 1,
	ended_at: null,
};
const TESTS: ShellJobInfo = {
	job_id: "job-2",
	command: "npm test",
	status: "exited",
	exit_code: 1,
	started_at: 2,
	ended_at: 3,
};

function emit(event: DaemonEventUnion): void {
	for (const handler of mocks.handlers) handler(event);
	flushSync();
}

function jobs(sessionId: string, list: ShellJobInfo[], seq = 5): DaemonEventUnion {
	return { type: "shell_jobs", session_id: sessionId, jobs: list, seq } as DaemonEventUnion;
}

function focus(sessionId: string): void {
	ingestEvent({ type: "session_state", session_id: sessionId, state: "idle", seq: 1 } as DaemonEventUnion);
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let stop: () => void = () => {};

beforeEach(() => {
	resetSession();
	resetRightPane();
	resetTaskLists();
	resetShellJobs();
	stop = startShellJobs();
	target = document.createElement("div");
	document.body.appendChild(target);
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	stop();
	target.remove();
});

describe("store", () => {
	it("keeps the newest list per session", () => {
		emit(jobs("s1", [DEV]));
		emit(jobs("s2", [TESTS]));
		emit(jobs("s1", [{ ...DEV, status: "stopped" }]));
		expect(shellJobsFor("s1").map((j) => j.status)).toEqual(["stopped"]);
		expect(shellJobsFor("s2")).toEqual([TESTS]);
		expect(shellJobsFor(null)).toEqual([]);
	});

	it("reads the daemon's fixture", () => {
		emit(fixtures.shell_jobs as DaemonEventUnion);
		expect(shellJobsFor("sess-1").map((j) => j.job_id)).toEqual(["job-1", "job-2"]);
	});

	it("labels each state in words", () => {
		expect(jobStateLabel(DEV)).toBe("running");
		expect(jobStateLabel(TESTS)).toBe("exited 1");
		expect(jobStateLabel({ ...DEV, status: "lost" })).toBe("lost");
	});
});

describe("Plan pane", () => {
	it("lists the focused session's jobs", () => {
		focus("s1");
		emit(jobs("s1", [DEV, TESTS]));
		emit(jobs("s2", [{ ...DEV, job_id: "job-9", command: "other" }]));
		component = mount(PlanPane, { target });
		flushSync();

		const section = target.querySelector('section[aria-label="Background jobs"]');
		expect(section?.textContent).toContain("Background jobs · 1 running");
		const rows = [...target.querySelectorAll("li.job")].map((li) =>
			li.textContent?.replace(/\s+/g, " ").trim(),
		);
		expect(rows).toEqual(["running npm run dev job-1", "exited 1 npm test job-2"]);
		// The empty state is for a pane with nothing in it.
		expect(target.textContent).not.toContain("lands here as it works");
	});

	it("shows no jobs section when the session has none", () => {
		focus("s1");
		component = mount(PlanPane, { target });
		flushSync();
		expect(target.querySelector('section[aria-label="Background jobs"]')).toBeNull();
	});
});
