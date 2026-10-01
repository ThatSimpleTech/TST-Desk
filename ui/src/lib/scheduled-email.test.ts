// @vitest-environment jsdom
//
// TD-3820: Deliver to → Email reveals one address field. Create sends it
// only for email. A failed delivery is named on the row without changing
// the run's own word.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, JobRunEntry } from "./protocol";
import { jobLastRun, jobRunLabel } from "./scheduled";

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

function job(over: Partial<JobEntry> & Pick<JobEntry, "id">): JobEntry {
	return {
		workspace: "/ws/proj",
		instruction: "Morning brief",
		cadence: "every 1 day",
		next_run: "2026-10-02T04:00:00+00:00",
		deliver_to: "window",
		paused: false,
		running: false,
		last_run: "2026-10-01T12:00:00+00:00",
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

function choose(select: HTMLSelectElement, value: string): void {
	select.value = value;
	select.dispatchEvent(new Event("change", { bubbles: true }));
}

function button(name: string): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].find(
		(el) => el.textContent?.trim() === name,
	);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
}

beforeEach(() => {
	mocks.sent.length = 0;
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

describe("email address on the job form", () => {
	it("shows the address only when deliver to is email", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		expect([...document.body.querySelectorAll("span")].some((el) => el.textContent === "Email address")).toBe(
			false,
		);
		choose(selectFor("Deliver to"), "email");
		await tick();
		const address = field("Email address");
		expect(address).toBeInstanceOf(HTMLInputElement);
		typeInto(field("Workspace"), "/ws/proj");
		typeInto(field("Instruction"), "Morning brief");
		typeInto(address, "owner@example.com");
		button("Create").click();
		await tick();
		expect(mocks.sent.at(-1)).toMatchObject({
			type: "save_job",
			deliver_to: "email",
			email_to: "owner@example.com",
		});
	});

	it("a window create does not send an address", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		typeInto(field("Workspace"), "/ws/proj");
		typeInto(field("Instruction"), "Morning brief");
		button("Create").click();
		await tick();
		const msg = mocks.sent.at(-1);
		expect(msg?.type).toBe("save_job");
		expect(msg).not.toHaveProperty("email_to");
	});
});

describe("delivery failure on the receipt", () => {
	it("names a failed send and leaves an ok run as Ran", () => {
		const when = "2026-10-01T12:00:00+00:00";
		const line = jobLastRun(
			job({
				id: "j1",
				last_status: "ok",
				last_delivery: "failed",
				last_delivery_error: "SMTPException: auth failed",
			}),
			"UTC",
		);
		expect(line.startsWith("Ran ")).toBe(true);
		expect(line).toContain("delivery failed (SMTPException: auth failed)");
		const run: JobRunEntry = {
			started_at: when,
			scheduled_for: when,
			trigger: "schedule",
			status: "failed",
			summary: "disk full",
			session_id: null,
			delivery: "failed",
			delivery_error: "",
		};
		expect(jobRunLabel(run, "UTC")).toContain("Failed ");
		expect(jobRunLabel(run, "UTC")).toContain("delivery failed");
		expect(jobRunLabel(run, "UTC")).not.toContain("delivery failed (");
	});
});
