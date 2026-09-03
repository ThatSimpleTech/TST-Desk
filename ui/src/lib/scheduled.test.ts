import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry } from "./protocol";
import {
	emptyDraft,
	formatLocal,
	jobFailed,
	jobLastRun,
	jobWhen,
	jobsEmptyCopy,
	saveFromDraft,
	saveFromJob,
} from "./scheduled";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
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

import {
	createJob,
	deleteScheduledJob,
	pauseJob,
	refreshJobs,
	resetScheduled,
	scheduled,
	setDraftField,
	startScheduled,
} from "./scheduled.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function job(over: Partial<JobEntry> & Pick<JobEntry, "id">): JobEntry {
	return {
		workspace: "/ws/proj",
		instruction: "summarize the inbox",
		cadence: "every 1 hour",
		next_run: null,
		deliver_to: "window",
		paused: false,
		last_run: null,
		last_status: null,
		last_summary: null,
		last_session_id: null,
		...over,
	};
}

beforeEach(() => {
	mocks.sent.length = 0;
	resetScheduled();
	startScheduled();
	mocks.sent.length = 0;
});

afterEach(() => {
	resetScheduled();
});

describe("copy and draft", () => {
	it("names an empty list", () => {
		expect(jobsEmptyCopy()).toMatch(/no scheduled jobs/i);
	});

	it("omits blank cadence and next_run on create", () => {
		expect(saveFromDraft(emptyDraft("/ws"))).toEqual({
			type: "save_job",
			workspace: "/ws",
			instruction: undefined,
			cadence: "every 1 hour",
			next_run: undefined,
			deliver_to: "window",
			paused: false,
		});
	});

	it("pause is save_job with paused flipped", () => {
		const row = job({ id: "j1", paused: false, next_run: "2026-08-21T18:00:00+00:00" });
		expect(saveFromJob(row, true)).toEqual({
			type: "save_job",
			id: "j1",
			workspace: "/ws/proj",
			instruction: "summarize the inbox",
			cadence: "every 1 hour",
			next_run: "2026-08-21T18:00:00+00:00",
			deliver_to: "window",
			paused: true,
		});
	});

	it("says paused before the next slot", () => {
		expect(jobWhen(job({ id: "j1", paused: true, next_run: "soon" }))).toBe("Paused");
		// next_run is stored UTC and shown local, so this must not be the raw
		// string. The exact text is the runner's locale; the year is not.
		const shown = jobWhen(job({ id: "j1", next_run: "2026-08-21T18:00:00+00:00" }), "UTC");
		expect(shown).not.toBe("2026-08-21T18:00:00+00:00");
		expect(shown).toContain("2026");
		expect(jobWhen(job({ id: "j1" }))).toBe("every 1 hour");
	});

	it("shows a stored UTC instant in the viewer's own zone", () => {
		const iso = "2026-08-21T18:00:00+00:00";
		// The whole point of formatting: two viewers on the same instant
		// read different wall-clock hours.
		expect(formatLocal(iso, "UTC")).not.toBe(formatLocal(iso, "Asia/Tokyo"));
	});

	it("shows an unparseable instant as-is rather than Invalid Date", () => {
		expect(formatLocal("soon")).toBe("soon");
		expect(formatLocal("soon")).not.toMatch(/invalid/i);
	});

	it("reports the last fire, and that there was none", () => {
		expect(jobLastRun(job({ id: "j1" }))).toBe("Never run");
		expect(jobFailed(job({ id: "j1" }))).toBe(false);

		const ok = job({ id: "j1", last_run: "2026-08-21T18:00:00+00:00", last_status: "ok" });
		expect(jobLastRun(ok, "UTC")).toMatch(/^Ran /);
		expect(jobFailed(ok)).toBe(false);

		const bad = job({ id: "j1", last_run: "2026-08-21T18:00:00+00:00", last_status: "failed" });
		expect(jobLastRun(bad, "UTC")).toMatch(/^Failed /);
		expect(jobFailed(bad)).toBe(true);
	});
});

describe("scheduled store", () => {
	it("lists jobs from job_list", () => {
		refreshJobs();
		expect(mocks.sent).toEqual([{ type: "list_jobs" }]);
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1" })],
		});
		expect(scheduled.items.map((row) => row.id)).toEqual(["j1"]);
		expect(scheduled.loading).toBe(false);
	});

	it("creates from draft fields, not NL", () => {
		setDraftField("workspace", "/ws/proj");
		setDraftField("instruction", "summarize the inbox");
		expect(createJob()).toBe(true);
		expect(mocks.sent.at(-1)).toEqual({
			type: "save_job",
			workspace: "/ws/proj",
			instruction: "summarize the inbox",
			cadence: "every 1 hour",
			next_run: undefined,
			deliver_to: "window",
			paused: false,
		});
	});

	it("pauses and deletes through the store verbs", () => {
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1" })] });
		expect(pauseJob("j1")).toBe(true);
		expect(mocks.sent.at(-1)).toMatchObject({ type: "save_job", id: "j1", paused: true });
		expect(deleteScheduledJob("j1")).toBe(true);
		expect(mocks.sent.at(-1)).toEqual({ type: "delete_job", job_id: "j1" });
	});

	it("does not invent a pause for an unknown id", () => {
		expect(pauseJob("missing")).toBe(false);
		expect(deleteScheduledJob("missing")).toBe(false);
		expect(mocks.sent).toEqual([]);
	});

	it("surfaces a typed job error", () => {
		emit({
			type: "error",
			seq: 1,
			code: "job_invalid",
			message: "missing instruction",
		});
		expect(scheduled.error).toBe("missing instruction");
		expect(scheduled.loading).toBe(false);
	});

	it("prefills an empty workspace hint once", () => {
		refreshJobs("/ws/hint");
		expect(scheduled.draft.workspace).toBe("/ws/hint");
		setDraftField("workspace", "/other");
		refreshJobs("/ws/hint");
		expect(scheduled.draft.workspace).toBe("/other");
	});

	it("clears the draft once the daemon acks the create, keeping the workspace", () => {
		setDraftField("workspace", "/ws/proj");
		setDraftField("instruction", "summarize the inbox");
		setDraftField("next_run", "2026-08-21T18:00:00+00:00");
		expect(createJob()).toBe(true);
		// Still filled: nothing has come back yet, so the create may still fail.
		expect(scheduled.draft.instruction).toBe("summarize the inbox");

		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1" })] });
		expect(scheduled.draft.instruction).toBe("");
		expect(scheduled.draft.next_run).toBe("");
		expect(scheduled.draft.workspace).toBe("/ws/proj");
	});

	it("keeps what the user typed when the create is rejected", () => {
		setDraftField("workspace", "/ws/proj");
		setDraftField("instruction", "summarize the inbox");
		createJob();
		emit({ type: "error", seq: 1, code: "job_invalid", message: "missing instruction" });
		expect(scheduled.draft.instruction).toBe("summarize the inbox");

		// And the rejected create must not arm a later, unrelated list refresh.
		emit({ type: "job_list", seq: 2, jobs: [] });
		expect(scheduled.draft.instruction).toBe("summarize the inbox");
	});

	it("does not clear the draft on a job_list nobody asked for", () => {
		setDraftField("instruction", "half-typed");
		refreshJobs();
		emit({ type: "job_list", seq: 1, jobs: [] });
		expect(scheduled.draft.instruction).toBe("half-typed");
	});
});
