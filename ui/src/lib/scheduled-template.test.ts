// @vitest-environment jsdom
//
// TD-3816: a template fills the job form and does not save a job.
// Save as template stores the form. Schedule this chat prefills from a session.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type {
	ClientMessageUnion,
	DaemonEventUnion,
	JobEntry,
	JobTemplateEntry,
	SaveJobTemplate,
} from "./protocol";
import type { SessionRow } from "./sessions.svelte.js";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	allow: true,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			if (mocks.handler === handler) mocks.handler = null;
		};
	},
	onDaemonEvent: () => () => {},
	onConnectionState: () => () => {},
	onResume: () => () => {},
	attachToSession: () => {},
	detachFromSession: () => {},
	ws: { state: "disconnected" },
	sendToDaemon: (msg: ClientMessageUnion) => {
		if (!mocks.allow) return false;
		mocks.sent.push(msg);
		return true;
	},
}));

import ScheduledPane from "./components/ScheduledPane.svelte";
import RailSessionRow from "./components/RailSessionRow.svelte";
import { projects, resetProjects } from "./projects.svelte.js";
import {
	applyTemplate,
	editJob,
	openScheduledFromSession,
	resetScheduled,
	saveAsTemplate,
	scheduled,
	setDraftField,
	setTemplateName,
	startScheduled,
} from "./scheduled.svelte.js";
import { emptyDraft } from "./scheduled.js";
import {
	draftFromSession,
	draftFromTemplate,
	resolveTemplateNextRun,
	saveTemplateFromDraft,
} from "./scheduled-template.js";
import { closeRowMenus, sessions } from "./sessions.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function template(
	over: Partial<JobTemplateEntry> & Pick<JobTemplateEntry, "id" | "name">,
): JobTemplateEntry {
	return {
		builtin: false,
		instruction: "",
		cadence: null,
		next_run: null,
		deliver_to: "window",
		grace: null,
		retries: 0,
		retry_delay: null,
		preset: null,
		engine: null,
		workspace: null,
		...over,
	};
}

const digest = template({
	id: "weekday-morning-digest",
	name: "Weekday morning digest",
	builtin: true,
	cadence: "weekdays at 7:45",
	grace: 7200,
	retries: 1,
	retry_delay: 600,
});

const reminder = template({
	id: "one-shot-reminder",
	name: "One-shot reminder",
	builtin: true,
	next_run: "tomorrow at 9:00",
});

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

function button(name: string): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].find(
		(el) => el.textContent?.trim() === name,
	);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
}

function row(sessionId: string): SessionRow {
	return {
		sessionId,
		workspacePath: "/ws/chat",
		state: "idle",
		updatedAt: "2026-09-29T12:00:00Z",
		archived: false,
		starred: false,
		title: "Inbox chat",
		preset: "budget",
		busy: false,
	};
}

beforeEach(() => {
	mocks.sent.length = 0;
	mocks.allow = true;
	mocks.handler = null;
	resetScheduled();
	resetProjects();
	closeRowMenus();
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
	resetProjects();
	closeRowMenus();
});

describe("template draft helpers", () => {
	it("keeps the folder on a built-in and resolves tomorrow locally", () => {
		const now = new Date(2026, 8, 29, 15, 0, 0);
		const current = { ...emptyDraft("/ws/already"), instruction: "typed" };
		const filled = draftFromTemplate(digest, current, now);
		expect(filled.workspace).toBe("/ws/already");
		expect(filled.instruction).toBe("");
		expect(filled.cadence).toBe("weekdays at 7:45");
		expect(filled.next_run).toBe("");
		expect(filled.grace).toBe("2 hours");
		expect(filled.retries).toBe("1");
		expect(filled.deliver_to).toBe("window");

		const oneShot = draftFromTemplate(reminder, current, now);
		const when = new Date(oneShot.next_run);
		expect(oneShot.cadence).toBe("");
		expect(when.getFullYear()).toBe(2026);
		expect(when.getMonth()).toBe(8);
		expect(when.getDate()).toBe(30);
		expect(when.getHours()).toBe(9);
		expect(when.getMinutes()).toBe(0);
		expect(resolveTemplateNextRun("tomorrow at 24:00", now)).toBe("tomorrow at 24:00");
		expect(resolveTemplateNextRun("2026-09-30T13:00:00.000Z", now)).toBe(
			"2026-09-30T13:00:00.000Z",
		);
	});

	it("prefills a chat as a new job and sends the form as a template", () => {
		const draft = draftFromSession({
			type: "session_job_source",
			seq: 1,
			session_id: "sess-1",
			workspace: "/ws/chat",
			preset: "vllm",
			engine: "grok",
			instruction: "first real",
		});
		expect(draft.workspace).toBe("/ws/chat");
		expect(draft.instruction).toBe("first real");
		expect(draft.preset).toBe("vllm");
		expect(draft.engine).toBe("grok");
		expect(draft.cadence).toBe("weekdays at 9:00");
		expect(draft.grace).toBe("");
		expect(draft.retries).toBe("");

		const payload = saveTemplateFromDraft("Inbox digest", {
			...draft,
			grace: "2 hours",
			retries: "1",
		});
		expect(payload).toEqual({
			type: "save_job_template",
			name: "Inbox digest",
			instruction: "first real",
			deliver_to: "window",
			cadence: "weekdays at 9:00",
			workspace: "/ws/chat",
			preset: "vllm",
			engine: "grok",
			grace: "2 hours",
			retries: 1,
			retry_delay: "10 minutes",
		});
	});
});

describe("Scheduled pane templates", () => {
	it("fills the draft from a template and does not save a job", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		typeInto(field("Workspace"), "/ws/already");
		typeInto(field("Instruction"), "typed");
		emit({ type: "job_templates", seq: 1, templates: [digest, reminder] });
		await tick();
		choose(selectFor("Start from template"), digest.id);
		await tick();
		expect(field("Workspace").value).toBe("/ws/already");
		expect(field("Instruction").value).toBe("");
		expect(field("Cadence").value).toBe("weekdays at 7:45");
		expect(field("Next run").value).toBe("");
		expect(selectFor("If late").value).toBe("2 hours");
		expect(selectFor("Retries").value).toBe("1");
		expect(mocks.sent.some((msg) => msg.type === "save_job")).toBe(false);

		emit({ type: "job_list", seq: 1, jobs: [job({ id: "j1" })] });
		expect(editJob("j1")).toBe(true);
		await tick();
		expect(button("Cancel")).toBeTruthy();
		choose(selectFor("Start from template"), reminder.id);
		await tick();
		expect(field("Cadence").value).toBe("");
		expect(field("Next run").value).not.toBe("");
		expect(scheduled.editingId).toBeNull();
		expect(document.body.textContent).not.toContain("Cancel");
		expect(mocks.sent.some((msg) => msg.type === "save_job")).toBe(false);
	});

	it("saves the form as a template and reloads it without creating a job", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({ type: "job_templates", seq: 1, templates: [digest, reminder] });
		await tick();
		choose(selectFor("Start from template"), digest.id);
		await tick();
		typeInto(field("Instruction"), "count the mail");
		typeInto(field("Template name"), "Inbox digest");
		const before = mocks.sent.length;
		button("Save as template").click();
		await tick();
		const saved = mocks.sent.at(-1);
		expect(saved?.type).toBe("save_job_template");
		const payload = saved as SaveJobTemplate;
		expect(payload.name).toBe("Inbox digest");
		expect(payload.instruction).toBe("count the mail");
		expect(payload.cadence).toBe("weekdays at 7:45");
		expect(payload.grace).toBe("2 hours");
		expect(payload.retries).toBe(1);
		expect(payload.retry_delay).toBe("10 minutes");
		expect(mocks.sent.some((msg) => msg.type === "save_job")).toBe(false);

		emit({ type: "job_list", seq: 1, jobs: [] });
		await tick();
		expect(field("Instruction").value).toBe("count the mail");

		const stored = template({
			id: "tmpl-1",
			name: "Inbox digest",
			instruction: "count the mail",
			cadence: "weekdays at 7:45",
			grace: 7200,
			retries: 1,
			retry_delay: 600,
		});
		emit({ type: "job_templates", seq: 1, templates: [digest, reminder, stored] });
		await tick();
		expect(field("Template name").value).toBe("");
		typeInto(field("Instruction"), "");
		await tick();
		expect(field("Instruction").value).toBe("");
		choose(selectFor("Start from template"), "tmpl-1");
		await tick();
		expect(field("Instruction").value).toBe("count the mail");
		expect(mocks.sent.length).toBe(before + 1);

		typeInto(field("Template name"), "   ");
		button("Save as template").click();
		await tick();
		expect(document.body.querySelector("[role='alert']")?.textContent).toBe("name is required");
		expect(mocks.sent.length).toBe(before + 1);
	});

	it("ignores a late session reply after a template is chosen", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		emit({ type: "job_templates", seq: 1, templates: [digest] });
		expect(openScheduledFromSession("sess-1")).toBe(true);
		expect(projects.surface).toBe("scheduled");
		expect(mocks.sent.at(-1)).toEqual({ type: "get_session_job_source", session_id: "sess-1" });
		emit({
			type: "session_job_source",
			seq: 1,
			session_id: "sess-1",
			workspace: "/ws/chat",
			preset: "vllm",
			engine: "grok",
			instruction: "first real",
		});
		await tick();
		expect(field("Instruction").value).toBe("first real");
		expect(field("Workspace").value).toBe("/ws/chat");
		expect(selectFor("Model preset").value).toBe("vllm");
		expect(selectFor("Engine").value).toBe("grok");
		expect(field("Cadence").value).toBe("weekdays at 9:00");

		applyTemplate(digest.id);
		await tick();
		emit({
			type: "session_job_source",
			seq: 1,
			session_id: "sess-1",
			workspace: "/ws/other",
			preset: "budget",
			engine: "native",
			instruction: "late reply",
		});
		await tick();
		expect(field("Cadence").value).toBe("weekdays at 7:45");
		expect(field("Instruction").value).toBe("");
		expect(field("Workspace").value).toBe("/ws/chat");
	});

	it("does not open Scheduled when the request never leaves", () => {
		mocks.allow = false;
		expect(openScheduledFromSession("sess-1")).toBe(false);
		expect(projects.surface).toBe("home");
		expect(mocks.sent).toEqual([]);
	});
});

describe("Schedule this chat", () => {
	it("is a row action that opens Scheduled for that session", async () => {
		const chat = row("sess-9");
		sessions.menuFor = chat.sessionId;
		app = mount(RailSessionRow, {
			target: document.body,
			props: { row: chat, active: false, onselect: () => {} },
		});
		await tick();
		button("Schedule this chat").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({
			type: "get_session_job_source",
			session_id: "sess-9",
		});
		expect(projects.surface).toBe("scheduled");
		expect(sessions.menuFor).toBeNull();
		expect(mocks.sent.some((msg) => msg.type === "save_job")).toBe(false);
	});
});

describe("template errors stay on the form", () => {
	it("shows a refusal and leaves the name the user typed", async () => {
		app = mount(ScheduledPane, { target: document.body });
		await tick();
		setDraftField("instruction", "count the mail");
		setTemplateName("Inbox digest");
		expect(saveAsTemplate()).toBe(true);
		emit({
			type: "error",
			seq: 1,
			code: "template_invalid",
			message: "Preset: 'gone' is not in the catalog",
		});
		await tick();
		expect(document.body.querySelector("[role='alert']")?.textContent).toContain(
			"not in the catalog",
		);
		expect(field("Template name").value).toBe("Inbox digest");
		expect(field("Instruction").value).toBe("count the mail");
	});
});
