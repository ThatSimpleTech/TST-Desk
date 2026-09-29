import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, JobRunEntry } from "./protocol";
import {
	emptyDraft,
	formatLocal,
	humanizeCadence,
	jobActivity,
	draftFromJob,
	graceDraftValue,
	retriesDraftValue,
	jobFailed,
	jobMeta,
	jobMissed,
	jobLastRun,
	jobRunLabel,
	jobWhen,
	jobsEmptyCopy,
	saveFromDraft,
	saveFromEdit,
	saveFromJob,
	sessionMissingCopy,
	viewerTimeZone,
	workspaceSuggestions,
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
	parseJobRequest,
	pauseJob,
	refreshJobs,
	runJob,
	resetScheduled,
	scheduled,
	setDraftField,
	setParseText,
	startScheduled,
	toggleJobHistory,
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
		running: false,
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
		expect(saveFromDraft(emptyDraft("/ws"), "UTC")).toEqual({
			type: "save_job",
			workspace: "/ws",
			instruction: undefined,
			cadence: "weekdays at 9:00",
			next_run: undefined,
			deliver_to: "window",
			paused: false,
			timezone: "UTC",
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
			timezone: undefined,
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

	it("says Running while a turn is in flight", () => {
		const row = job({ id: "j1", last_run: "2026-08-21T18:00:00+00:00", last_status: "ok", running: true });
		expect(jobActivity(row)).toBe("Running…");
		expect(jobActivity(job({ id: "j1" }))).toBe("Never run");
	});
});

describe("time zone", () => {
	it("sends the given zone on create", () => {
		expect(saveFromDraft(emptyDraft("/ws"), "America/Chicago").timezone).toBe("America/Chicago");
	});

	it("defaults to the viewer's own zone", () => {
		expect(saveFromDraft(emptyDraft("/ws")).timezone).toBe(viewerTimeZone());
	});

	it("passes a job's zone through Pause/Resume unchanged", () => {
		expect(saveFromJob(job({ id: "j1", timezone: "Asia/Tokyo" }), true).timezone).toBe("Asia/Tokyo");
		expect(saveFromJob(job({ id: "j1", timezone: null }), true).timezone).toBeNull();
		expect(saveFromJob(job({ id: "j1" }), true).timezone).toBeUndefined();
	});
});

describe("humanizeCadence", () => {
	const cases: [string, string | null | undefined, string][] = [
		["45 7 * * 1-5", "America/Chicago", "Weekdays at 7:45 AM"],
		["0 9 * * *", "America/Chicago", "Daily at 9:00 AM"],
		["0 0 * * *", "UTC", "Daily at 12:00 AM"],
		["30 12 * * 0,6", "UTC", "Weekends at 12:30 PM"],
		["30 17 * * 6,0", "UTC", "Weekends at 5:30 PM"],
		["0 8 * * 1", "UTC", "Mondays at 8:00 AM"],
		["0 8 * * 7", "UTC", "Sundays at 8:00 AM"],
		["0 8 * * 1,3,5", "UTC", "Mon, Wed and Fri at 8:00 AM"],
		["0 8 * * 1,2,3,4,5", "UTC", "Weekdays at 8:00 AM"],
		["45 7 * * 1-5", null, "Weekdays at 7:45 AM UTC"],
		["45 7 * * 1-5", undefined, "Weekdays at 7:45 AM UTC"],
		["every 2 hours", "UTC", "every 2 hours"],
		["*/15 * * * *", "UTC", "*/15 * * * *"],
		["0 9 1 * *", "UTC", "0 9 1 * *"],
		["0 9 * 6 *", "UTC", "0 9 * 6 *"],
		["0 9 * * MON", "UTC", "0 9 * * MON"],
		["0 9 * * 8", "UTC", "0 9 * * 8"],
		["60 9 * * *", "UTC", "60 9 * * *"],
		["0 24 * * *", "UTC", "0 24 * * *"],
		["0 9 * *", "UTC", "0 9 * *"],
	];
	it.each(cases)("%s (%s) -> %s", (cadence, tz, expected) => {
		expect(humanizeCadence(cadence, tz)).toBe(expected);
	});

	it("shows the words beside the next fire, not twice", () => {
		const row = job({ id: "j1", cadence: "45 7 * * 1-5", timezone: "UTC", deliver_to: "slack" });
		expect(jobMeta(row, "UTC")).toBe("Weekdays at 7:45 AM · slack");
	});

	it("adds the cadence after a concrete next run", () => {
		const row = job({ id: "j1", cadence: "45 7 * * 1-5", timezone: "UTC", next_run: "2026-08-21T07:45:00+00:00" });
		const meta = jobMeta(row, "UTC").split(" · ");
		expect(meta).toHaveLength(3);
		expect(meta[1]).toBe("Weekdays at 7:45 AM");
		expect(meta[2]).toBe("window");
	});
});

describe("workspaceSuggestions", () => {
	it("lists pinned first and drops repeats", () => {
		expect(workspaceSuggestions(["/a", "/b"], ["/b", "/c"])).toEqual(["/a", "/b", "/c"]);
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
		setDraftField("grace", "6 hours");
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
		expect(scheduled.draft.grace).toBe("6 hours");
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
			cadence: "weekdays at 9:00",
			next_run: undefined,
			deliver_to: "window",
			paused: false,
			timezone: viewerTimeZone(),
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
		expect(runJob("missing")).toBe(false);
		expect(mocks.sent).toEqual([]);
	});

	it("sends run_job and renders a running row", () => {
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1" })] });
		setDraftField("instruction", "half-typed");
		expect(runJob("j1")).toBe(true);
		expect(mocks.sent.at(-1)).toEqual({ type: "run_job", job_id: "j1" });
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", running: true })] });
		const row = scheduled.items[0];
		if (row === undefined) throw new Error("missing row");
		expect(row.running).toBe(true);
		expect(jobActivity(row)).toBe("Running…");
		expect(scheduled.loading).toBe(false);
		expect(scheduled.error).toBeNull();
		// A list pushed because a run started is not a create ack.
		expect(scheduled.draft.instruction).toBe("half-typed");
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

	it("surfaces run-now refusals on the same path", () => {
		for (const code of ["job_running", "job_not_found"]) {
			emit({ type: "error", seq: 1, code, message: code });
			expect(scheduled.error).toBe(code);
			expect(scheduled.loading).toBe(false);
		}
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

function run(over: Partial<JobRunEntry> = {}): JobRunEntry {
	return {
		started_at: "2026-08-21T15:00:00+00:00",
		scheduled_for: "2026-08-21T12:00:00+00:00",
		trigger: "schedule",
		status: "ok",
		summary: "digest",
		session_id: "sess-1",
		...over,
	};
}

describe("run history (TD-3811)", () => {
	it("names Ran or Failed, and says manual only for Run now", () => {
		const scheduledRun = jobRunLabel(run(), "UTC");
		expect(scheduledRun).toMatch(/^Ran /);
		expect(scheduledRun.toLowerCase()).not.toContain("manual");
		expect(scheduledRun.toLowerCase()).not.toContain("schedule");
		const manual = jobRunLabel(
			run({ trigger: "manual", status: "failed", scheduled_for: null }),
			"UTC",
		);
		expect(manual).toMatch(/^Failed /);
		expect(manual.endsWith(" · manual")).toBe(true);
		expect(sessionMissingCopy()).toBe("Session no longer exists");
	});

	it("asks for the log when history opens and stores the reply", () => {
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1", last_run: "2026-08-21T15:00:00+00:00" })],
		});
		toggleJobHistory("j1");
		expect(mocks.sent.at(-1)).toEqual({ type: "list_job_runs", job_id: "j1" });
		expect(scheduled.historyOpen.j1).toBe(true);
		expect(scheduled.historySeen.j1).toBe("2026-08-21T15:00:00+00:00");
		expect(scheduled.runs.j1).toBeUndefined();
		expect(scheduled.loading).toBe(false);

		const rows = [
			run({ started_at: "2026-08-21T16:00:00+00:00", trigger: "manual", scheduled_for: null }),
			run(),
		];
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: rows });
		expect(scheduled.runs.j1).toEqual(rows);

		const before = mocks.sent.length;
		toggleJobHistory("j1");
		expect(scheduled.historyOpen.j1).toBe(false);
		expect(mocks.sent).toHaveLength(before);
	});

	it("asks again when an open job's last run moves, including off never-run", () => {
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", last_run: null })] });
		toggleJobHistory("j1");
		expect(scheduled.historySeen.j1).toBeNull();
		mocks.sent.length = 0;

		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", last_run: null })] });
		expect(mocks.sent).toEqual([]);

		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1", last_run: "2026-08-21T16:00:00+00:00" })],
		});
		expect(mocks.sent).toEqual([{ type: "list_job_runs", job_id: "j1" }]);
		expect(scheduled.historySeen.j1).toBe("2026-08-21T16:00:00+00:00");
		expect(scheduled.historyOpen.j1).toBe(true);
	});

	it("does not ask again for a disclosure that is closed", () => {
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", last_run: null })] });
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1", last_run: "2026-08-21T16:00:00+00:00" })],
		});
		expect(mocks.sent.filter((msg) => msg.type === "list_job_runs")).toEqual([]);
	});

	it("drops history for a job that left the list and ignores a late reply", () => {
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1" }), job({ id: "j2" })],
		});
		toggleJobHistory("j1");
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: [run()] });
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j2" })] });
		expect(scheduled.historyOpen.j1).toBeUndefined();
		expect(scheduled.runs.j1).toBeUndefined();
		expect(scheduled.historyOpen.j2).toBeUndefined();
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: [run()] });
		expect(scheduled.runs.j1).toBeUndefined();
	});

	it("keeps an open history across a save ack, and refetches if the receipt moved", () => {
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1", last_run: "2026-08-21T15:00:00+00:00" })],
		});
		toggleJobHistory("j1");
		setDraftField("workspace", "/ws/proj");
		setDraftField("instruction", "summarize the inbox");
		expect(createJob()).toBe(true);
		mocks.sent.length = 0;
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1", last_run: "2026-08-21T17:00:00+00:00" })],
		});
		expect(mocks.sent).toEqual([{ type: "list_job_runs", job_id: "j1" }]);
		expect(scheduled.historyOpen.j1).toBe(true);
		expect(scheduled.draft.instruction).toBe("");
	});

	it("clears history when the store resets", () => {
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1" })] });
		toggleJobHistory("j1");
		emit({ type: "job_runs", seq: 1, job_id: "j1", runs: [run()] });
		resetScheduled();
		expect(scheduled.historyOpen).toEqual({});
		expect(scheduled.historySeen).toEqual({});
		expect(scheduled.runs).toEqual({});
	});
});

describe("grace (TD-3813)", () => {
	it("names a missed slot without calling it a failure", () => {
		const missed = job({
			id: "j1",
			last_run: "2026-08-21T18:00:00+00:00",
			last_status: "missed",
		});
		expect(jobLastRun(missed, "UTC")).toMatch(/^Missed /);
		expect(jobFailed(missed)).toBe(false);
		expect(jobMissed(missed)).toBe(true);
		expect(jobMissed(job({ id: "j1", last_status: "failed" }))).toBe(false);
		const label = jobRunLabel(run({ status: "missed" }), "UTC");
		expect(label).toMatch(/^Missed /);
		expect(label).not.toMatch(/Failed/);
		expect(label.toLowerCase()).not.toContain("manual");
	});

	it("maps stored seconds to the phrase the form sends", () => {
		expect(graceDraftValue(null)).toBe("");
		expect(graceDraftValue(undefined)).toBe("");
		expect(graceDraftValue(7200)).toBe("2 hours");
		expect(graceDraftValue(3600)).toBe("1 hour");
		expect(graceDraftValue(5400)).toBe("90 minutes");
		expect(graceDraftValue(86400)).toBe("1 day");
		expect(graceDraftValue(172800)).toBe("2 days");
		expect(graceDraftValue(90)).toBe("90");

		const row = job({ id: "j1", grace: 7200, cadence: "every 1 hour" });
		expect(draftFromJob(row).grace).toBe("2 hours");
		expect(saveFromEdit(draftFromJob(row), row, "UTC").grace).toBe("2 hours");
		expect(saveFromEdit({ ...draftFromJob(row), grace: "" }, row, "UTC").grace).toBe("");
		expect(saveFromJob(row, true)).not.toHaveProperty("grace");
		expect(saveFromDraft({ ...emptyDraft("/ws"), grace: "2 hours" }, "UTC").grace).toBe("2 hours");
		expect(saveFromDraft(emptyDraft("/ws"), "UTC")).not.toHaveProperty("grace");
	});
});

describe("retries (TD-3814)", () => {
	it("maps a stored count and sends None as omit or clear", () => {
		expect(retriesDraftValue(null)).toBe("");
		expect(retriesDraftValue(undefined)).toBe("");
		expect(retriesDraftValue(0)).toBe("");
		expect(retriesDraftValue(2)).toBe("2");
		expect(retriesDraftValue(4)).toBe("4");

		const row = job({ id: "j1", retries: 2, retry_delay: 600, cadence: "every 1 hour" });
		expect(draftFromJob(row).retries).toBe("2");
		const kept = saveFromEdit(draftFromJob(row), row, "UTC");
		expect(kept.retries).toBe(2);
		expect(kept.retry_delay).toBe("10 minutes");
		const cleared = saveFromEdit({ ...draftFromJob(row), retries: "" }, row, "UTC");
		expect(cleared.retries).toBe(0);
		expect(cleared.retry_delay).toBe("");
		expect(saveFromJob(row, true)).not.toHaveProperty("retries");
		expect(saveFromJob(row, true)).not.toHaveProperty("retry_delay");

		const created = saveFromDraft({ ...emptyDraft("/ws"), retries: "1" }, "UTC");
		expect(created.retries).toBe(1);
		expect(created.retry_delay).toBe("10 minutes");
		expect(saveFromDraft(emptyDraft("/ws"), "UTC")).not.toHaveProperty("retries");
	});

	it("names the try on a scheduled history row and not on Run now", () => {
		const labeled = jobRunLabel(run({ attempt: 2, attempts: 3 }), "UTC");
		expect(labeled).toContain("attempt 2 of 3");
		expect(labeled.toLowerCase()).not.toContain("manual");
		const manual = jobRunLabel(
			run({ trigger: "manual", status: "failed", scheduled_for: null }),
			"UTC",
		);
		expect(manual.endsWith(" · manual")).toBe(true);
		expect(manual.toLowerCase()).not.toContain("attempt");
	});
});
