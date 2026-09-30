// @vitest-environment jsdom
//
// TD-3813: If late on the job form, and Missed on the row and the history.
// The select sends a phrase. Blank is omitted on create and "" on edit.
// Pause does not send grace. A skipped slot is not a failed turn.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, JobRunEntry, SaveJob } from "./protocol";

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

function selectFor(label: string): HTMLSelectElement {
	const found = [...document.body.querySelectorAll("label")].find(
		(el) => el.querySelector("span")?.textContent === label,
	);
	const select = found?.querySelector("select");
	if (!(select instanceof HTMLSelectElement)) throw new Error(`missing select ${label}`);
	return select;
}

function choose(select: HTMLSelectElement, value: string): void {
	select.value = value;
	select.dispatchEvent(new Event("change", { bubbles: true }));
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

function field(label: string): HTMLInputElement | HTMLTextAreaElement {
	const found = [...document.body.querySelectorAll("label")].find(
		(el) => el.querySelector("span")?.textContent === label,
	);
	const input = found?.querySelector("input, textarea");
	if (!(input instanceof HTMLInputElement) && !(input instanceof HTMLTextAreaElement)) {
		throw new Error(`missing field ${label}`);
	}
	return input;
}

function typeInto(input: HTMLInputElement | HTMLTextAreaElement, value: string): void {
	input.value = value;
	input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
	mocks.sent.length = 0;
	mocks.selectRow.mockClear();
	mocks.sessions.rows = [];
	mocks.handler = null;
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

describe("If late (TD-3813)", () => {
	it("defaults to always run and sends a phrase only when one is chosen", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		const late = selectFor("If late");
		expect([...late.options].map((option) => option.text)).toEqual([
			"Always run",
			"Skip if more than 30 min late",
			"Skip if more than 1 h late",
			"Skip if more than 2 h late",
			"Skip if more than 6 h late",
		]);
		expect([...late.options].map((option) => option.value)).toEqual([
			"",
			"30 minutes",
			"1 hour",
			"2 hours",
			"6 hours",
		]);
		expect(late.value).toBe("");

		typeInto(field("Workspace"), "/ws/proj");
		typeInto(field("Instruction"), "summarize the inbox");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("grace");

		choose(late, "2 hours");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave().grace).toBe("2 hours");
	});

	it("loads stored seconds, clears with Always run, and pause omits grace", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", grace: 7200 })] });
		await tick();
		button("Edit").click();
		await tick();
		expect(selectFor("If late").value).toBe("2 hours");

		choose(selectFor("If late"), "");
		await tick();
		button("Save").click();
		await tick();
		expect(lastSave().grace).toBe("");

		mocks.sent.length = 0;
		button("Pause").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("grace");
		expect(lastSave().paused).toBe(true);
	});

	it("keeps an unlisted grace on the form so Save does not clear it", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", grace: 5400 })] });
		await tick();
		button("Edit").click();
		await tick();
		const late = selectFor("If late");
		expect(late.value).toBe("90 minutes");
		expect([...late.options].map((option) => option.value)).toContain("90 minutes");
		button("Save").click();
		await tick();
		expect(lastSave().grace).toBe("90 minutes");
	});

	it("keeps the chosen grace when a sentence fills the rest of the draft", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		choose(selectFor("If late"), "6 hours");
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
		expect(selectFor("If late").value).toBe("6 hours");
	});

	it("renders Missed on the row and in history, apart from Failed", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({
			type: "job_list",
			seq: 1,
			jobs: [
				job({
					id: "late",
					instruction: "morning digest",
					last_status: "missed",
					last_summary: "Skipped the 7:45 AM run — 10 h late",
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

		const missed = document.querySelector(".card-missed");
		const failed = document.querySelector(".card-failed");
		if (!(missed instanceof HTMLElement) || !(failed instanceof HTMLElement)) {
			throw new Error("missing status cards");
		}
		expect(missed.classList.contains("card-failed")).toBe(false);
		expect(failed.classList.contains("card-missed")).toBe(false);
		expect(missed.querySelector(".run-missed")?.textContent ?? "").toMatch(/^Missed /);
		expect(missed.querySelector(".run-failed")).toBeNull();
		expect(failed.querySelector(".run-failed")?.textContent ?? "").toMatch(/^Failed /);
		expect(failed.querySelector(".run-missed")).toBeNull();

		button("History", missed).click();
		await tick();
		const missedRun: JobRunEntry = {
			started_at: "2026-08-21T22:45:00+00:00",
			scheduled_for: "2026-08-21T12:45:00+00:00",
			trigger: "schedule",
			status: "missed",
			summary: "Skipped the 7:45 AM run — 10 h late",
			session_id: null,
		};
		emit({ type: "job_runs", seq: 1, job_id: "late", runs: [missedRun] });
		await tick();
		const when = document.querySelector(".card-missed .missed");
		expect(when?.textContent ?? "").toMatch(/^Missed /);
		expect(when?.classList.contains("failed")).toBe(false);
		expect(when?.textContent ?? "").not.toMatch(/Failed/);
		expect(when?.textContent ?? "").not.toMatch(/manual/);
	});
});
