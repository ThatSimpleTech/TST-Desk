// @vitest-environment jsdom
//
// TD-3814: Retries on the job form, and attempt numbers in history.
// The select sends a count. None is omitted on create and 0 on edit.
// Pause does not send retries. The delay is the 10-minute phrase.

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

describe("Retries (TD-3814)", () => {
	it("defaults to none and sends a count with a 10-minute delay", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		const retries = selectFor("Retries");
		expect([...retries.options].map((option) => option.text)).toEqual(["None", "1", "2", "3"]);
		expect([...retries.options].map((option) => option.value)).toEqual(["", "1", "2", "3"]);
		expect(retries.value).toBe("");
		expect(document.body.textContent ?? "").toContain("after 10 min");

		typeInto(field("Workspace"), "/ws/proj");
		typeInto(field("Instruction"), "summarize the inbox");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("retries");
		expect(lastSave()).not.toHaveProperty("retry_delay");

		choose(retries, "2");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave().retries).toBe(2);
		expect(lastSave().retry_delay).toBe("10 minutes");
	});

	it("loads a stored count, clears with None, and pause omits retries", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "j1", retries: 2, retry_delay: 600, attempt: 0 })],
		});
		await tick();
		button("Edit").click();
		await tick();
		expect(selectFor("Retries").value).toBe("2");

		choose(selectFor("Retries"), "");
		await tick();
		button("Save").click();
		await tick();
		expect(lastSave().retries).toBe(0);
		expect(lastSave().retry_delay).toBe("");

		mocks.sent.length = 0;
		button("Pause").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("retries");
		expect(lastSave()).not.toHaveProperty("retry_delay");
		expect(lastSave().paused).toBe(true);
	});

	it("keeps an unlisted count on the form so Save does not clear it", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", retries: 4 })] });
		await tick();
		button("Edit").click();
		await tick();
		const retries = selectFor("Retries");
		expect(retries.value).toBe("4");
		expect([...retries.options].map((option) => option.value)).toContain("4");
		button("Save").click();
		await tick();
		expect(lastSave().retries).toBe(4);
		expect(lastSave().retry_delay).toBe("10 minutes");
	});

	it("keeps the chosen retries when a sentence fills the rest of the draft", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		choose(selectFor("Retries"), "3");
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
		expect(selectFor("Retries").value).toBe("3");
	});

	it("names the attempt on a history row", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ id: "inbox", instruction: "morning digest" })],
		});
		await tick();
		const card = document.querySelector(".card");
		if (!(card instanceof HTMLElement)) throw new Error("missing card");
		button("History", card).click();
		await tick();
		const run: JobRunEntry = {
			started_at: "2026-08-21T18:10:00+00:00",
			scheduled_for: "2026-08-21T18:00:00+00:00",
			trigger: "schedule",
			status: "ok",
			summary: "digest ready",
			session_id: null,
			attempt: 2,
			attempts: 3,
		};
		emit({ type: "job_runs", seq: 1, job_id: "inbox", runs: [run] });
		await tick();
		expect(card.textContent ?? "").toContain("attempt 2 of 3");
		expect(card.textContent ?? "").not.toMatch(/manual/);
	});
});
