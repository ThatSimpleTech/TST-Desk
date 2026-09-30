// @vitest-environment jsdom
//
// TD-3817: Then run on the job form, the row's follow-on, and chained history.
// Create omits None. Save sends the id, including "" to clear. Pause omits it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, JobRunEntry, SaveJob } from "./protocol";
import { draftFromJob, emptyDraft, jobMeta, jobRunLabel, saveFromDraft, saveFromEdit, saveFromJob } from "./scheduled";
import { draftFromTemplate } from "./scheduled-template";

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
		last_run: null,
		last_status: null,
		last_summary: null,
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

function card(instruction: string): HTMLElement {
	const found = [...document.querySelectorAll(".card")].find(
		(el) => el.querySelector(".card-name")?.textContent === instruction,
	);
	if (!(found instanceof HTMLElement)) throw new Error(`missing card ${instruction}`);
	return found;
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

function lastSave(): SaveJob {
	const msg = [...mocks.sent].reverse().find((item) => item.type === "save_job");
	if (msg?.type !== "save_job") throw new Error("expected save_job");
	return msg;
}

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

describe("Then run (TD-3817)", () => {
	it("names the follow-on on the row and in history, and not on a slot", () => {
		const digest = job({ id: "digest", then: "follow" });
		const follow = job({ id: "follow", instruction: "draft the follow-up" });
		expect(jobMeta(digest, "UTC", [digest, follow])).toContain("→ draft the follow-up");
		expect(jobMeta(digest, "UTC")).toContain("→ follow");
		expect(jobMeta(job({ id: "digest" }), "UTC")).not.toContain("→");

		const chained = jobRunLabel(
			run({ trigger: "chained", scheduled_for: null, note: "after digest" }),
			"UTC",
		);
		expect(chained).toMatch(/^Ran /);
		expect(chained).toContain("· chained · after digest");
		expect(chained.toLowerCase()).not.toContain("manual");
		const missed = jobRunLabel(
			run({
				trigger: "chained",
				status: "missed",
				scheduled_for: null,
				summary: "paused",
				note: "after digest",
				session_id: null,
			}),
			"UTC",
		);
		expect(missed).toMatch(/^Missed /);
		expect(missed).toContain("· chained · after digest");
		expect(missed.toLowerCase()).not.toContain("manual");
		const numbered = jobRunLabel(
			run({
				trigger: "chained",
				scheduled_for: null,
				note: "after digest",
				attempt: 1,
				attempts: 2,
			}),
			"UTC",
		);
		expect(numbered).toContain("· chained · after digest · attempt 1 of 2");
		expect(jobRunLabel(run({ note: "after digest" }), "UTC")).not.toContain("after");
	});

	it("omits None on create, sends the id on save, and pause omits it", () => {
		const row = job({ id: "digest", then: "follow" });
		const blank = { ...emptyDraft("/ws"), instruction: "summarize" };
		expect(saveFromDraft(blank, "UTC")).not.toHaveProperty("then");
		expect(saveFromDraft({ ...blank, then: "follow" }, "UTC").then).toBe("follow");
		expect(draftFromJob(row).then).toBe("follow");
		expect(saveFromEdit(draftFromJob(row), row, "UTC").then).toBe("follow");
		expect(saveFromEdit({ ...draftFromJob(row), then: "" }, row, "UTC").then).toBe("");
		expect(saveFromJob(row, true)).not.toHaveProperty("then");
		expect(
			draftFromTemplate(
				{
					id: "weekday-morning-digest",
					name: "Weekday morning digest",
					builtin: true,
					instruction: "digest",
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
				{ ...emptyDraft("/ws"), then: "follow" },
			).then,
		).toBe("");
	});

	it("lists the other jobs, keeps a missing id, and shows the chain on the card", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({
			type: "job_list",
			seq: 1,
			jobs: [
				job({ id: "digest", instruction: "summarize the inbox", then: "follow" }),
				job({ id: "follow", instruction: "draft the follow-up" }),
				job({ id: "stale", instruction: "old link", then: "gone" }),
			],
		});
		await tick();

		expect(card("summarize the inbox").querySelector(".card-meta")?.textContent ?? "").toContain(
			"→ draft the follow-up",
		);
		const creating = selectFor("Then run");
		expect([...creating.options].map((option) => option.text)).toEqual([
			"None",
			"summarize the inbox",
			"draft the follow-up",
			"old link",
		]);
		expect(creating.value).toBe("");

		typeInto(field("Workspace"), "/ws/proj");
		typeInto(field("Instruction"), "file the notes");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave()).not.toHaveProperty("then");

		choose(selectFor("Then run"), "follow");
		await tick();
		button("Create").click();
		await tick();
		expect(lastSave().then).toBe("follow");

		emit({
			type: "job_draft",
			seq: 1,
			ok: true,
			workspace: "/ws/proj",
			instruction: "from a sentence",
			cadence: "every 2 hours",
			deliver_to: "slack",
			paused: false,
		});
		await tick();
		expect(selectFor("Then run").value).toBe("follow");

		button("Edit", card("summarize the inbox")).click();
		await tick();
		const editing = selectFor("Then run");
		expect([...editing.options].map((option) => option.value)).toEqual(["", "follow", "stale"]);
		expect([...editing.options].map((option) => option.text)).not.toContain("summarize the inbox");
		expect(editing.value).toBe("follow");

		choose(editing, "");
		await tick();
		button("Save").click();
		await tick();
		expect(lastSave().then).toBe("");
		expect(lastSave().id).toBe("digest");

		choose(selectFor("Then run"), "follow");
		await tick();
		button("Save").click();
		await tick();
		expect(lastSave().then).toBe("follow");

		button("Pause", card("summarize the inbox")).click();
		await tick();
		expect(lastSave()).not.toHaveProperty("then");
		expect(lastSave().paused).toBe(true);

		button("Edit", card("old link")).click();
		await tick();
		const stale = selectFor("Then run");
		expect([...stale.options].map((option) => option.value)).toContain("gone");
		expect(stale.value).toBe("gone");

		button("History", card("draft the follow-up")).click();
		await tick();
		emit({
			type: "job_runs",
			seq: 1,
			job_id: "follow",
			runs: [
				run({
					trigger: "chained",
					scheduled_for: null,
					summary: "draft ready",
					note: "after digest",
					session_id: "sess-chain",
				}),
			],
		});
		await tick();
		const text = card("draft the follow-up").textContent ?? "";
		expect(text).toContain("chained");
		expect(text).toContain("after digest");
		expect(text).not.toContain("manual");
	});
});
