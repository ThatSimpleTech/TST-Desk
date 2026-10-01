// @vitest-environment jsdom
//
// TD-3819: Max run time on the job form. The select sends a phrase.
// Blank is omitted on create and "" on edit. Pause does not send it.
// A sentence does not clear a choice the user already made.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, SaveJob } from "./protocol";

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

function button(name: string): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].find((el) => el.textContent?.trim() === name);
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

describe("Max run time (TD-3819)", () => {
	it("defaults to the configured limit and sends a phrase only when one is chosen", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		const limit = selectFor("Max run time");
		expect([...limit.options].map((option) => option.text)).toEqual([
			"Default",
			"5 min",
			"10 min",
			"15 min",
			"30 min",
			"60 min",
		]);
		expect([...limit.options].map((option) => option.value)).toEqual([
			"",
			"5 minutes",
			"10 minutes",
			"15 minutes",
			"30 minutes",
			"60 minutes",
		]);
		expect(limit.value).toBe("");

		typeInto(field("Workspace"), "/ws/proj");
		typeInto(field("Instruction"), "summarize the inbox");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("max_run");

		choose(limit, "30 minutes");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave().max_run).toBe("30 minutes");
	});

	it("loads stored seconds, clears with Default, and pause omits the limit", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", max_run: 900 })] });
		await tick();
		button("Edit").click();
		await tick();
		expect(selectFor("Max run time").value).toBe("15 minutes");

		choose(selectFor("Max run time"), "");
		await tick();
		button("Save").click();
		await tick();
		expect(lastSave().max_run).toBe("");

		mocks.sent.length = 0;
		button("Pause").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("max_run");
		expect(lastSave().paused).toBe(true);
	});

	it("keeps an unlisted limit on the form so Save does not clear it", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1", max_run: 1200 })] });
		await tick();
		button("Edit").click();
		await tick();
		const limit = selectFor("Max run time");
		expect(limit.value).toBe("20 minutes");
		expect([...limit.options].map((option) => option.value)).toContain("20 minutes");
		button("Save").click();
		await tick();
		expect(lastSave().max_run).toBe("20 minutes");
	});

	it("keeps the chosen limit when a sentence fills the rest of the draft", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		choose(selectFor("Max run time"), "60 minutes");
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
		expect(selectFor("Max run time").value).toBe("60 minutes");
	});
});
