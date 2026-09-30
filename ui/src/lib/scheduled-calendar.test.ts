// @vitest-environment jsdom
//
// TD-3818: a local calendar on the job form, and Skipped (calendar) on the
// row and the history. Blank path and match are omitted on create and sent
// as "" on edit. Pause omits both. A calendar skip is not a failed turn.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, JobRunEntry, SaveJob } from "./protocol";
import { draftFromTemplate } from "./scheduled-template";
import { emptyDraft, jobFailed, jobMissed, saveFromDraft, saveFromEdit, saveFromJob } from "./scheduled";

const dialog = vi.hoisted(() => ({
	open: vi.fn<(options: unknown) => Promise<string | string[] | null>>(),
}));

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	selectRow: vi.fn<(sessionId: string) => void>(),
	sessions: { rows: [] as { sessionId: string }[] },
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
	open: dialog.open,
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
import { resetScheduled, startScheduled } from "./scheduled.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function job(over: Partial<JobEntry> & Pick<JobEntry, "id">): JobEntry {
	return {
		workspace: "/ws/proj",
		instruction: "summarize the inbox",
		cadence: "every 1 hour",
		next_run: "2026-08-24T12:45:00+00:00",
		deliver_to: "window",
		paused: false,
		running: false,
		last_run: "2026-08-21T22:45:00+00:00",
		last_status: "ok",
		last_summary: "digest",
		last_session_id: null,
		...over,
	};
}

function labelFor(name: string): HTMLElement {
	const found = [...document.body.querySelectorAll("label")].find(
		(el) => el.querySelector("span")?.textContent === name,
	);
	if (!(found instanceof HTMLElement)) throw new Error(`missing label ${name}`);
	return found;
}

function field(name: string): HTMLInputElement | HTMLTextAreaElement {
	const input = labelFor(name).querySelector("input, textarea");
	if (!(input instanceof HTMLInputElement) && !(input instanceof HTMLTextAreaElement)) {
		throw new Error(`missing field ${name}`);
	}
	return input;
}

function typeInto(input: HTMLInputElement | HTMLTextAreaElement, value: string): void {
	input.value = value;
	input.dispatchEvent(new Event("input", { bubbles: true }));
}

function button(name: string, root: ParentNode = document.body): HTMLButtonElement {
	const found = [...root.querySelectorAll("button")].find((el) => el.textContent?.trim() === name);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
}

function lastSave(): SaveJob {
	const msg = mocks.sent.at(-1);
	if (msg?.type !== "save_job") throw new Error("expected save_job");
	return msg;
}

beforeEach(() => {
	mocks.sent.length = 0;
	mocks.selectRow.mockClear();
	mocks.sessions.rows = [];
	mocks.handler = null;
	dialog.open.mockReset();
	resetScheduled();
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
});

describe("Skip days in calendar (TD-3818)", () => {
	it("omits a blank calendar on create and sends one when it is filled", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		expect(labelFor("Skip days in calendar").textContent ?? "").toContain("Skip days in calendar");
		expect(labelFor("Only events matching").textContent ?? "").toContain("Only events matching");
		expect(field("Only events matching").placeholder).toBe("holiday|PTO|OOO");

		typeInto(field("Workspace"), "/ws/proj");
		typeInto(field("Instruction"), "summarize the inbox");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("skip_calendar");
		expect(lastSave()).not.toHaveProperty("skip_match");

		typeInto(field("Skip days in calendar"), "/Users/me/holidays.ics");
		typeInto(field("Only events matching"), "holiday|PTO|OOO");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave().skip_calendar).toBe("/Users/me/holidays.ics");
		expect(lastSave().skip_match).toBe("holiday|PTO|OOO");
	});

	it("picks an .ics file from the native dialog", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		dialog.open.mockResolvedValue("/Users/me/holidays.ics");
		button("Browse…", labelFor("Skip days in calendar")).click();
		await vi.dynamicImportSettled();
		await tick();
		expect(dialog.open).toHaveBeenCalledWith({
			directory: false,
			multiple: false,
			title: "Skip days in calendar",
			filters: [{ name: "Calendar", extensions: ["ics"] }],
		});
		expect(field("Skip days in calendar").value).toBe("/Users/me/holidays.ics");
	});

	it("clears with an empty string on edit, and pause omits the calendar", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		const row = job({
			id: "j1",
			skip_calendar: "/Users/me/holidays.ics",
			skip_match: "holiday|PTO",
		});
		emit({ type: "job_list", seq: 1, jobs: [row] });
		await tick();
		button("Edit").click();
		await tick();
		expect(field("Skip days in calendar").value).toBe("/Users/me/holidays.ics");
		expect(field("Only events matching").value).toBe("holiday|PTO");

		typeInto(field("Skip days in calendar"), "");
		typeInto(field("Only events matching"), "");
		await tick();
		button("Save").click();
		await tick();
		expect(lastSave().skip_calendar).toBe("");
		expect(lastSave().skip_match).toBe("");

		mocks.sent.length = 0;
		button("Pause").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("skip_calendar");
		expect(lastSave()).not.toHaveProperty("skip_match");
		expect(lastSave().paused).toBe(true);
	});

	it("renders Skipped (calendar) on the row and in history", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		const title = "ZQ-Holiday-Title";
		emit({
			type: "job_list",
			seq: 1,
			jobs: [
				job({
					id: "off",
					instruction: "morning digest",
					last_status: "skipped",
					last_summary: "calendar",
				}),
				job({
					id: "bad",
					instruction: "evening digest",
					last_status: "failed",
					last_summary: "disk full",
				}),
			],
		});
		await tick();

		const skipped = document.querySelector(".card-skipped");
		const failed = document.querySelector(".card-failed");
		if (!(skipped instanceof HTMLElement) || !(failed instanceof HTMLElement)) {
			throw new Error("missing status cards");
		}
		expect(skipped.classList.contains("card-failed")).toBe(false);
		expect(skipped.classList.contains("card-missed")).toBe(false);
		expect(failed.classList.contains("card-skipped")).toBe(false);
		expect(skipped.querySelector(".run-skipped")?.textContent ?? "").toMatch(/^Skipped /);
		expect(skipped.textContent ?? "").not.toContain(title);
		expect(skipped.querySelector(".run-failed")).toBeNull();
		expect(skipped.querySelector(".run-missed")).toBeNull();
		expect(jobMissed(job({ id: "off", last_status: "skipped" }))).toBe(false);
		expect(jobFailed(job({ id: "off", last_status: "skipped" }))).toBe(false);

		button("History", skipped).click();
		await tick();
		const skippedRun: JobRunEntry = {
			started_at: "2026-12-25T15:00:00+00:00",
			scheduled_for: "2026-12-25T13:00:00+00:00",
			trigger: "schedule",
			status: "skipped",
			summary: "calendar",
			session_id: null,
		};
		emit({ type: "job_runs", seq: 1, job_id: "off", runs: [skippedRun] });
		await tick();
		const when = document.querySelector(".card-skipped .skipped");
		expect(when?.textContent ?? "").toMatch(/^Skipped /);
		expect(when?.textContent ?? "").toContain("Skipped (calendar)");
		expect(when?.classList.contains("failed")).toBe(false);
		expect(when?.classList.contains("missed")).toBe(false);
		expect(when?.textContent ?? "").not.toMatch(/manual/);
		expect(document.body.textContent ?? "").not.toContain(title);
	});

	it("keeps the calendar when a sentence fills the rest of the draft", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		typeInto(field("Skip days in calendar"), "/Users/me/holidays.ics");
		typeInto(field("Only events matching"), "PTO");
		await tick();
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
		await tick();
		expect(field("Skip days in calendar").value).toBe("/Users/me/holidays.ics");
		expect(field("Only events matching").value).toBe("PTO");
	});
});

describe("calendar draft helpers", () => {
	it("round-trips the path and clears it from a template", () => {
		const row = job({
			id: "j1",
			skip_calendar: "/Users/me/holidays.ics",
			skip_match: "holiday|PTO",
		});
		const draft = {
			...emptyDraft("/ws"),
			skip_calendar: row.skip_calendar ?? "",
			skip_match: row.skip_match ?? "",
		};
		expect(saveFromDraft(draft, "UTC").skip_calendar).toBe("/Users/me/holidays.ics");
		expect(saveFromEdit({ ...draft, skip_calendar: "", skip_match: "" }, row, "UTC").skip_calendar).toBe(
			"",
		);
		expect(saveFromJob(row, true)).not.toHaveProperty("skip_calendar");
		const cleared = draftFromTemplate(
			{
				id: "weekday-morning-digest",
				name: "Weekday morning digest",
				builtin: true,
				instruction: "",
				cadence: "weekdays at 7:45",
				next_run: null,
				deliver_to: "window",
				grace: 7200,
				retries: 1,
				retry_delay: 600,
				preset: null,
				engine: null,
				workspace: null,
			},
			draft,
		);
		expect(cleared.skip_calendar).toBe("");
		expect(cleared.skip_match).toBe("");
		expect(cleared.grace).toBe("2 hours");
	});
});
