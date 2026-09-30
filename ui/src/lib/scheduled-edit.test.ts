import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, SaveJob } from "./protocol";
import { draftFromJob, jobFormCopy, saveFromEdit, viewerTimeZone } from "./scheduled";

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
	cancelEdit,
	deleteScheduledJob,
	editJob,
	pauseJob,
	resetScheduled,
	runJob,
	saveEdit,
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
		next_run: "2026-08-21T18:00:00+00:00",
		deliver_to: "window",
		paused: false,
		running: false,
		last_run: "2026-08-21T15:00:00+00:00",
		last_status: "ok",
		last_summary: "three new messages",
		last_session_id: "sess-1",
		timezone: "America/Chicago",
		...over,
	};
}

function lastSave(): SaveJob {
	const msg = mocks.sent.at(-1);
	if (msg === undefined || msg.type !== "save_job") throw new Error("expected save_job");
	return msg;
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

describe("job form copy", () => {
	it("names a create and an edit", () => {
		expect(jobFormCopy(false)).toEqual({
			title: "New job",
			lede: "Parse a sentence, edit the draft, then create. Cadence or next run, not both.",
			submit: "Create",
		});
		expect(jobFormCopy(true)).toEqual({
			title: "Edit job",
			lede: "Change the fields, then save. Cadence or next run, not both.",
			submit: "Save",
		});
	});
});

describe("draftFromJob / saveFromEdit", () => {
	it("loads a cadence job without its armed slot", () => {
		const row = job({ id: "j1" });
		expect(draftFromJob(row)).toMatchObject({
			workspace: "/ws/proj",
			instruction: "summarize the inbox",
			cadence: "every 1 hour",
			next_run: "",
			deliver_to: "window",
		});
		const payload = saveFromEdit(draftFromJob(row), row, "UTC");
		expect(payload.id).toBe("j1");
		expect(payload.cadence).toBe("every 1 hour");
		expect(payload.next_run).toBeUndefined();
		expect(payload.timezone).toBeUndefined();
	});

	it("loads a one-shot with a blank cadence and sends the clear", () => {
		const row = job({ id: "once", cadence: null, next_run: "2026-09-30T15:00:00+00:00" });
		expect(draftFromJob(row).cadence).toBe("");
		expect(draftFromJob(row).next_run).toBe("2026-09-30T15:00:00+00:00");
		const payload = saveFromEdit(draftFromJob(row), row, "UTC");
		expect(payload.cadence).toBe("");
		expect(payload.next_run).toBe("2026-09-30T15:00:00+00:00");
		expect(payload.id).toBe("once");
	});

	it("sends next_run only when the draft has no cadence", () => {
		const row = job({ id: "j1" });
		const typed = { ...draftFromJob(row), next_run: "2026-09-30T15:00:00+00:00" };
		expect(saveFromEdit(typed, row, "UTC").next_run).toBeUndefined();
		const switched = { ...draftFromJob(row), cadence: "", next_run: "2026-09-30T15:00:00+00:00" };
		const payload = saveFromEdit(switched, row, "UTC");
		expect(payload.cadence).toBe("");
		expect(payload.next_run).toBe("2026-09-30T15:00:00+00:00");
	});

	const zoneCases: [string | null | undefined, string | null, string, string | undefined][] = [
		[null, "45 7 * * 1-5", "45 7 * * 1-5", undefined],
		[undefined, "45 7 * * 1-5", "45 7 * * 1-5", undefined],
		[null, "45 7 * * 1-5", "0 8 * * 1-5", "America/Chicago"],
		["UTC", "45 7 * * 1-5", "0 8 * * 1-5", undefined],
		["America/Chicago", "45 7 * * 1-5", "0 8 * * 1-5", undefined],
		[null, null, "", undefined],
		[null, null, "daily at 9", "America/Chicago"],
	];
	it.each(zoneCases)(
		"zone %s cadence %s -> %s sends %s",
		(storedZone, storedCadence, draftCadence, sentZone) => {
			const row = job({
				id: "j1",
				timezone: storedZone,
				cadence: storedCadence,
				next_run: storedCadence ? "2026-08-21T12:45:00+00:00" : "2026-09-30T15:00:00+00:00",
			});
			const draft = { ...draftFromJob(row), cadence: draftCadence };
			expect(saveFromEdit(draft, row, "America/Chicago").timezone).toBe(sentZone);
		},
	);
});

describe("edit in the store", () => {
	const row = () => job({ id: "j1", cadence: "45 7 * * 1-5", timezone: null });

	it("loads the draft, and Save sends the id", () => {
		emit({ type: "job_list", seq: 1, jobs: [row()] });
		expect(editJob("missing")).toBe(false);
		expect(editJob("j1")).toBe(true);
		expect(scheduled.editingId).toBe("j1");
		expect(scheduled.draft.instruction).toBe("summarize the inbox");
		expect(scheduled.draft.cadence).toBe("45 7 * * 1-5");
		expect(scheduled.draft.next_run).toBe("");
		expect(scheduled.draft.workspace).toBe("/ws/proj");
		expect(mocks.sent).toEqual([]);

		setDraftField("instruction", "count the mail");
		expect(saveEdit()).toBe(true);
		const payload = lastSave();
		expect(payload.id).toBe("j1");
		expect(payload.instruction).toBe("count the mail");
		expect(payload.cadence).toBe("45 7 * * 1-5");
		expect(payload.next_run).toBeUndefined();
		expect(payload.timezone).toBeUndefined();
		// Still editing until the daemon acks — a refusal must keep the text.
		expect(scheduled.editingId).toBe("j1");
		expect(scheduled.draft.instruction).toBe("count the mail");
	});

	it("adopts the viewer zone only when a legacy cadence changes", () => {
		emit({ type: "job_list", seq: 1, jobs: [row()] });
		editJob("j1");
		setDraftField("cadence", "0 8 * * 1-5");
		saveEdit();
		expect(lastSave().timezone).toBe(viewerTimeZone());
		expect(lastSave().id).toBe("j1");
	});

	it("Cancel restores the new-job draft and keeps the workspace", () => {
		emit({ type: "job_list", seq: 1, jobs: [row()] });
		editJob("j1");
		setDraftField("instruction", "half-edited");
		cancelEdit();
		expect(scheduled.editingId).toBeNull();
		expect(scheduled.draft.instruction).toBe("");
		expect(scheduled.draft.cadence).toBe("weekdays at 9:00");
		expect(scheduled.draft.next_run).toBe("");
		expect(scheduled.draft.workspace).toBe("/ws/proj");
		expect(saveEdit()).toBe(false);
	});

	it("keeps the draft while Run now, Pause and another delete run", () => {
		emit({
			type: "job_list",
			seq: 1,
			jobs: [row(), job({ id: "j2", instruction: "other" })],
		});
		editJob("j1");
		setDraftField("instruction", "half-edited");
		expect(runJob("j1")).toBe(true);
		emit({
			type: "job_list",
			seq: 2,
			jobs: [job({ ...row(), running: true }), job({ id: "j2", instruction: "other" })],
		});
		expect(scheduled.editingId).toBe("j1");
		expect(scheduled.draft.instruction).toBe("half-edited");
		expect(pauseJob("j1")).toBe(true);
		emit({
			type: "job_list",
			seq: 3,
			jobs: [job({ ...row(), paused: true }), job({ id: "j2", instruction: "other" })],
		});
		expect(scheduled.draft.instruction).toBe("half-edited");
		expect(deleteScheduledJob("j2")).toBe(true);
		emit({ type: "job_list", seq: 4, jobs: [job({ ...row(), paused: true })] });
		expect(scheduled.editingId).toBe("j1");
		expect(scheduled.draft.instruction).toBe("half-edited");
		// Pause landed on the row while the form was open; Save must not undo it.
		saveEdit();
		expect(lastSave().paused).toBe(true);
		expect(lastSave().id).toBe("j1");
	});

	it("resets when the edited row is deleted, and when the list drops it", () => {
		emit({ type: "job_list", seq: 1, jobs: [row()] });
		editJob("j1");
		setDraftField("instruction", "half-edited");
		expect(deleteScheduledJob("j1")).toBe(true);
		expect(mocks.sent.at(-1)).toEqual({ type: "delete_job", job_id: "j1" });
		expect(scheduled.editingId).toBeNull();
		expect(scheduled.draft.instruction).toBe("");
		expect(scheduled.draft.cadence).toBe("weekdays at 9:00");
		expect(scheduled.draft.workspace).toBe("/ws/proj");

		emit({ type: "job_list", seq: 2, jobs: [row()] });
		editJob("j1");
		setDraftField("instruction", "again");
		emit({ type: "job_list", seq: 3, jobs: [] });
		expect(scheduled.editingId).toBeNull();
		expect(scheduled.draft.instruction).toBe("");
		expect(scheduled.draft.cadence).toBe("weekdays at 9:00");
	});

	it("returns to the new-job draft once the daemon acks the save", () => {
		emit({ type: "job_list", seq: 1, jobs: [row()] });
		editJob("j1");
		setDraftField("instruction", "count the mail");
		saveEdit();
		emit({ type: "error", seq: 2, code: "job_invalid", message: "Cadence or next run is required" });
		expect(scheduled.editingId).toBe("j1");
		expect(scheduled.draft.instruction).toBe("count the mail");
		saveEdit();
		emit({ type: "job_list", seq: 3, jobs: [job({ ...row(), instruction: "count the mail" })] });
		expect(scheduled.editingId).toBeNull();
		expect(scheduled.draft.instruction).toBe("");
		expect(scheduled.draft.cadence).toBe("weekdays at 9:00");
		expect(scheduled.draft.workspace).toBe("/ws/proj");
	});
});
