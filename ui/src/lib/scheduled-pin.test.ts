// @vitest-environment jsdom
//
// TD-3812: the job form pins a catalog preset and an engine. Create omits
// a blank pin (use current). Edit always sends the fields, including ""
// to clear. Pause omits them so a removed catalog name can still be paused.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry, SaveJob } from "./protocol";

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

import ScheduledPinFields from "./components/ScheduledPinFields.svelte";
import {
	draftFromJob,
	jobMeta,
	saveFromDraft,
	saveFromEdit,
	saveFromJob,
} from "./scheduled";
import { createJob, editJob, resetScheduled, saveEdit, setDraftField, startScheduled } from "./scheduled.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function job(over: Partial<JobEntry> = {}): JobEntry {
	return {
		id: "j1",
		workspace: "/ws/proj",
		instruction: "summarize the inbox",
		cadence: "45 7 * * 1-5",
		next_run: null,
		deliver_to: "slack",
		paused: false,
		timezone: "UTC",
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

function lastSave(): SaveJob {
	const msg = mocks.sent.at(-1);
	if (msg?.type !== "save_job") throw new Error("expected save_job");
	return msg;
}

beforeEach(() => {
	mocks.sent.length = 0;
	resetScheduled();
	resetSettings();
	settings.presets = ["budget", "vllm"];
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
	resetSettings();
});

describe("pin on the wire", () => {
	it("shows a pinned preset and engine on the row", () => {
		expect(jobMeta(job({ preset: "vllm", engine: "grok" }), "UTC")).toBe(
			"Weekdays at 7:45 AM · slack · vllm · grok",
		);
		expect(jobMeta(job(), "UTC")).toBe("Weekdays at 7:45 AM · slack");
	});

	it("omits a blank pin on create and always sends it on edit", () => {
		const blank = saveFromDraft(
			{
				workspace: "/ws",
				instruction: "summarize",
				cadence: "every 1 hour",
				next_run: "",
				deliver_to: "window",
				paused: false,
				preset: "",
				engine: "",
				grace: "",
				retries: "",
				then: "",
				skip_calendar: "",
				skip_match: "",
				max_run: "",
			},
			"UTC",
		);
		expect(blank.preset).toBeUndefined();
		expect(blank.engine).toBeUndefined();

		const pinned = saveFromDraft(
			{
				workspace: "/ws",
				instruction: "summarize",
				cadence: "every 1 hour",
				next_run: "",
				deliver_to: "window",
				paused: false,
				preset: "vllm",
				engine: "grok",
				grace: "",
				retries: "",
				then: "",
				skip_calendar: "",
				skip_match: "",
				max_run: "",
			},
			"UTC",
		);
		expect(pinned.preset).toBe("vllm");
		expect(pinned.engine).toBe("grok");
	});

	it("loads the pin, and pause does not resend it", () => {
		const row = job({ preset: "vllm", engine: "native" });
		expect(draftFromJob(row).preset).toBe("vllm");
		expect(draftFromJob(row).engine).toBe("native");
		expect(draftFromJob(job()).preset).toBe("");
		expect(draftFromJob(job()).engine).toBe("");

		const pause = saveFromJob(row, true);
		expect(pause).not.toHaveProperty("preset");
		expect(pause).not.toHaveProperty("engine");

		const kept = saveFromEdit(draftFromJob(row), row, "UTC");
		expect(kept.preset).toBe("vllm");
		expect(kept.engine).toBe("native");
		const cleared = saveFromEdit({ ...draftFromJob(row), preset: "", engine: "" }, row, "UTC");
		expect(cleared.preset).toBe("");
		expect(cleared.engine).toBe("");
	});
});

describe("pin selects", () => {
	it("sends the chosen preset and engine on create", async () => {
		app = mount(ScheduledPinFields, { target: document.body });
		await tick();

		const preset = selectFor("Model preset");
		const engine = selectFor("Engine");
		expect([...preset.options].map((option) => option.text)).toEqual([
			"Use current",
			"budget",
			"vllm",
		]);
		expect([...engine.options].map((option) => option.text)).toEqual([
			"Use current",
			"Native",
			"Grok",
		]);
		expect([...engine.options].map((option) => option.value)).toEqual(["", "native", "grok"]);

		choose(preset, "vllm");
		choose(engine, "grok");
		await tick();
		setDraftField("workspace", "/ws/proj");
		setDraftField("instruction", "summarize the inbox");
		expect(createJob()).toBe(true);
		expect(lastSave()).toMatchObject({ preset: "vllm", engine: "grok" });
	});

	it("loads a pin into the selects and sends a clear", async () => {
		app = mount(ScheduledPinFields, { target: document.body });
		await tick();
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ preset: "vllm", engine: "native" })],
		});
		expect(editJob("j1")).toBe(true);
		await tick();
		expect(selectFor("Model preset").value).toBe("vllm");
		expect(selectFor("Engine").value).toBe("native");

		choose(selectFor("Model preset"), "");
		await tick();
		expect(saveEdit()).toBe(true);
		const payload = lastSave();
		expect(payload.preset).toBe("");
		expect(payload.engine).toBe("native");
		expect(payload.id).toBe("j1");
	});

	it("keeps a retired preset visible on the edit form", async () => {
		app = mount(ScheduledPinFields, { target: document.body });
		await tick();
		emit({
			type: "job_list",
			seq: 1,
			jobs: [job({ preset: "retired", engine: "grok" })],
		});
		expect(editJob("j1")).toBe(true);
		await tick();
		const preset = selectFor("Model preset");
		expect([...preset.options].map((option) => option.value)).toContain("retired");
		expect(preset.value).toBe("retired");
		expect(selectFor("Engine").value).toBe("grok");
	});
});
