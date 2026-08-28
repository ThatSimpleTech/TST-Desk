import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry } from "./protocol";
import { emptyDraft, jobWhen, jobsEmptyCopy, saveFromDraft, saveFromJob } from "./scheduled";

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
	parseJobRequest,
	pauseJob,
	refreshJobs,
	resetScheduled,
	scheduled,
	setDraftField,
	setParseText,
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
		expect(jobWhen(job({ id: "j1", next_run: "2026-08-21T18:00:00+00:00" }))).toBe(
			"2026-08-21T18:00:00+00:00",
		);
		expect(jobWhen(job({ id: "j1" }))).toBe("every 1 hour");
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

	it("parses NL into the draft without saving", () => {
		setParseText("every 2 hours in /ws/proj summarize the inbox deliver to slack");
		expect(parseJobRequest()).toBe(true);
		expect(mocks.sent.at(-1)).toEqual({
			type: "parse_job",
			text: "every 2 hours in /ws/proj summarize the inbox deliver to slack",
		});
		emit({
			type: "job_draft",
			seq: 1,
			ok: true,
			workspace: "/ws/proj",
			instruction: "summarize the inbox",
			cadence: "every 2 hours",
			deliver_to: "slack",
			paused: false,
		});
		expect(scheduled.draft.workspace).toBe("/ws/proj");
		expect(scheduled.draft.instruction).toBe("summarize the inbox");
		expect(scheduled.draft.cadence).toBe("every 2 hours");
		expect(scheduled.draft.deliver_to).toBe("slack");
		expect(scheduled.items).toEqual([]);
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
});
