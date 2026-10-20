// @vitest-environment jsdom
//
// TD-5122: the native loop's task list in the window. The store keeps the
// newest `task_list` per session; the Plan pane shows the focused one, and
// the strip above the composer (the phone's view) shows it while anything
// is left to do. The socket is mocked so events arrive as the daemon's.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { DaemonEventUnion, TaskListItem } from "./protocol";
import fixtures from "./protocol-fixtures.json";

const mocks = vi.hoisted(() => ({
	handlers: [] as ((e: DaemonEventUnion) => void)[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handlers.push(handler);
		return () => {
			mocks.handlers = mocks.handlers.filter((h) => h !== handler);
		};
	},
	sendToDaemon: () => true,
}));

import PlanPane from "./components/PlanPane.svelte";
import TaskStrip from "./components/chat/TaskStrip.svelte";
import { resetRightPane, rightPane } from "./right-pane.svelte.js";
import { ingestEvent, resetSession } from "./session-status.svelte.js";
import { currentTask, progressLabel, showTaskStrip, statusLabel } from "./task-list";
import {
	pinnedPlanFor,
	resetTaskLists,
	startTaskLists,
	taskListFor,
	taskLists,
} from "./task-list-store.svelte.js";

const PLAN: TaskListItem[] = [
	{ content: "Read the failing test", status: "done" },
	{ content: "Fix the date parser", status: "in_progress" },
	{ content: "Run the suite", status: "pending" },
];

function emit(event: DaemonEventUnion): void {
	for (const handler of mocks.handlers) handler(event);
	flushSync();
}

function taskList(sessionId: string, items: TaskListItem[], seq = 5): DaemonEventUnion {
	return { type: "task_list", session_id: sessionId, items, seq } as DaemonEventUnion;
}

function focus(sessionId: string): void {
	ingestEvent({ type: "session_state", session_id: sessionId, state: "idle", seq: 1 } as DaemonEventUnion);
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let stop: () => void = () => {};

beforeEach(() => {
	resetSession();
	resetRightPane();
	resetTaskLists();
	stop = startTaskLists();
	target = document.createElement("div");
	document.body.appendChild(target);
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	stop();
	target.remove();
});

describe("helpers", () => {
	it("say how far along the list is", () => {
		expect(progressLabel(PLAN)).toBe("1 of 3 done");
		expect(progressLabel(PLAN.map((t) => ({ ...t, status: "done" as const })))).toBe("All 3 done");
	});

	it("name the item in progress, else the next pending one", () => {
		expect(currentTask(PLAN)?.content).toBe("Fix the date parser");
		expect(currentTask([PLAN[0], PLAN[2]])?.content).toBe("Run the suite");
		expect(currentTask([PLAN[0]])).toBeNull();
	});

	it("label each status in words", () => {
		expect(PLAN.map((t) => statusLabel(t.status))).toEqual(["Done", "In progress", "Pending"]);
	});

	it("hide the strip once nothing is left", () => {
		expect(showTaskStrip(PLAN)).toBe(true);
		expect(showTaskStrip([PLAN[0]])).toBe(false);
		expect(showTaskStrip([])).toBe(false);
	});
});

describe("store", () => {
	it("keeps the newest list per session", () => {
		focus("s1");
		emit(taskList("s1", PLAN));
		emit(taskList("s2", [PLAN[2]]));
		expect(taskListFor("s1")).toEqual(PLAN);
		expect(taskListFor("s2")).toEqual([PLAN[2]]);

		emit(taskList("s1", []));
		expect(taskListFor("s1")).toEqual([]);
		expect(taskListFor(null)).toEqual([]);
	});

	it("reads the daemon's fixture", () => {
		emit(fixtures.task_list as DaemonEventUnion);
		expect(taskLists.bySession["sess-1"]?.map((t) => t.status)).toEqual([
			"done",
			"in_progress",
			"pending",
		]);
	});

	it("opens the Plan pane when the focused chat starts a list, not on each update", () => {
		focus("s1");
		emit(taskList("s2", PLAN));
		expect(rightPane.tab).toBe("activity");

		emit(taskList("s1", PLAN));
		expect(rightPane.tab).toBe("plan");

		rightPane.tab = "files";
		emit(taskList("s1", PLAN.map((t) => ({ ...t, status: "done" as const }))));
		expect(rightPane.tab).toBe("files");
	});
});

describe("Plan pane", () => {
	it("shows the focused chat's native list with its progress", () => {
		focus("s1");
		emit(taskList("s1", PLAN));
		component = mount(PlanPane, { target });
		flushSync();

		const pane = target.querySelector('section[aria-label="Task list"]');
		expect(pane?.textContent).toContain("Tasks · 1 of 3 done");
		const rows = [...target.querySelectorAll("li")].map((li) => li.textContent?.replace(/\s+/g, " ").trim());
		expect(rows).toEqual([
			"✓ Read the failing test Done",
			"▸ Fix the date parser In progress",
			"○ Run the suite Pending",
		]);
		// Grok's approval is for Grok's plan; a task list has nothing to approve.
		expect(target.querySelector("button")).toBeNull();
	});

	it("follows each update and keeps another chat's list out", () => {
		focus("s1");
		emit(taskList("s2", PLAN));
		component = mount(PlanPane, { target });
		flushSync();
		expect(target.querySelector('section[aria-label="Task list"]')).toBeNull();

		emit(taskList("s1", [{ content: "Write the test", status: "in_progress" }]));
		expect(target.textContent).toContain("Write the test");
		expect(target.textContent).toContain("0 of 1 done");
	});
});

describe("pinned plan (TD-5123)", () => {
	function pinned(sessionId: string, plan: string): DaemonEventUnion {
		return { type: "plan_pinned", session_id: sessionId, plan, seq: 9 } as DaemonEventUnion;
	}

	it("keeps the newest approved plan per session", () => {
		emit(fixtures.plan_pinned as DaemonEventUnion);
		expect(pinnedPlanFor("sess-1")).toBe("1. Add greet().");
		emit(pinned("sess-1", "1. Add greet() and a test."));
		expect(pinnedPlanFor("sess-1")).toBe("1. Add greet() and a test.");
		expect(pinnedPlanFor("s2")).toBe("");
	});

	it("shows above the task list in the Plan pane, with nothing to approve", () => {
		focus("s1");
		emit(pinned("s1", "## Plan\n1. Add `greet()`."));
		emit(taskList("s1", PLAN));
		emit(pinned("s2", "another chat's plan"));
		component = mount(PlanPane, { target });
		flushSync();

		const sections = [...target.querySelectorAll("section")].map((s) => s.getAttribute("aria-label"));
		expect(sections).toEqual(["Approved plan", "Task list"]);
		const plan = target.querySelector('section[aria-label="Approved plan"]');
		expect(plan?.textContent).toContain("Approved plan · pinned");
		expect(plan?.querySelector("h2")?.textContent).toBe("Plan");
		expect(plan?.textContent).toContain("greet()");
		expect(target.textContent).not.toContain("another chat's plan");
		expect(target.querySelector("button")).toBeNull();
	});
});

describe("task strip", () => {
	function render(items: TaskListItem[]): void {
		component = mount(TaskStrip, { target, props: { items } });
		flushSync();
	}

	it("is one line with the progress and the item in progress until opened", () => {
		render(PLAN);
		const head = target.querySelector<HTMLButtonElement>('button[aria-label="Task list"]');
		expect(head).not.toBeNull();
		expect(head?.getAttribute("aria-expanded")).toBe("false");
		expect(head?.textContent).toContain("Tasks · 1 of 3 done");
		expect(head?.textContent).toContain("Fix the date parser");
		expect(target.querySelectorAll("li")).toHaveLength(0);

		head?.click();
		flushSync();
		expect(head?.getAttribute("aria-expanded")).toBe("true");
		expect(target.querySelectorAll("li")).toHaveLength(3);
		const describedBy = head?.getAttribute("aria-describedby") ?? "";
		expect(document.getElementById(describedBy)?.textContent).toContain("1 of 3 done");
	});

	it("renders nothing without an unfinished list", () => {
		render([]);
		expect(target.querySelector("section")).toBeNull();
		unmount(component!);
		render(PLAN.map((t) => ({ ...t, status: "done" as const })));
		expect(target.querySelector("section")).toBeNull();
	});
});
