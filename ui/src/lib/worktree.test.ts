// @vitest-environment jsdom
//
// TD-5126: a git worktree per session. The rail shows the branch and closes
// the worktree (keep, merge back or remove); a fresh chat's start screen says
// what the worktree lacks and offers the main checkout instead.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type {
	ClientMessageUnion,
	DaemonEventUnion,
	SessionList,
	SessionWorktreeInfo,
	WorktreeState,
} from "./protocol";
import fixtures from "./protocol-fixtures.json";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
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
		mocks.sent.push(msg);
		return true;
	},
}));

import RailSessionRow from "./components/RailSessionRow.svelte";
import WorktreeNotice from "./components/chat/WorktreeNotice.svelte";
import { CLOSE_WORKTREE_ACTION, rowActions } from "./rail";
import { closeRowMenus, resetSessions, sessions, startSessions } from "./sessions.svelte.js";
import { worktreeNotice } from "./worktree-notice";

const WT: SessionWorktreeInfo = {
	branch: "tst/wt/1a2b3c4d",
	path: "/data/worktrees/project-1a2b3c4d",
	main_dirty: 0,
	setup: "none",
	setup_note: "",
};

let app: ReturnType<typeof mount> | null = null;
let stop: (() => void) | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function list(worktree: SessionWorktreeInfo | null): SessionList {
	return {
		type: "session_list",
		seq: 1,
		sessions: [
			{
				session_id: "sess-1",
				workspace_path: "/home/user/project",
				state: "idle",
				created_at: "2026-10-20T10:00:00Z",
				updated_at: "2026-10-20T10:00:00Z",
				event_count: 3,
				archived: false,
				starred: false,
				title: "Fix the parser",
				worktree,
			},
		],
	};
}

function button(name: string): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].find(
		(el) => el.textContent?.trim() === name,
	);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
}

function hasButton(name: string): boolean {
	return [...document.body.querySelectorAll("button")].some((el) => el.textContent?.trim() === name);
}

beforeEach(() => {
	mocks.sent.length = 0;
	resetSessions();
	stop = startSessions();
});

afterEach(async () => {
	if (app !== null) {
		await unmount(app);
		app = null;
	}
	stop?.();
	stop = null;
	document.body.replaceChildren();
	resetSessions();
});

describe("the wire", () => {
	it("carries the worktree on a session row and a worktree_state event", () => {
		const listed = fixtures.session_list_worktree as SessionList;
		emit(listed);
		const row = sessions.rows[0];
		expect(row.workspacePath).toBe("/home/user/project");
		expect(row.worktree?.branch).toBe("tst/wt/sess-1ab");
		expect(row.worktree?.main_dirty).toBe(2);

		const state = fixtures.worktree_state as WorktreeState;
		expect(state.worktree?.setup).toBe("failed");
		mocks.sent.length = 0;
		emit(state);
		// The list is the truth: a worktree change asks for it again.
		expect(mocks.sent).toEqual([{ type: "list_sessions" }]);
		expect((fixtures.worktree_state_closed as WorktreeState).worktree).toBeNull();
		expect(fixtures.close_worktree).toEqual({ type: "close_worktree", session_id: "sess-1", action: null });
		expect(fixtures.close_worktree_merge.action).toBe("merge");
	});

	it("a row in its folder has no worktree", () => {
		emit(fixtures.session_list as SessionList);
		expect(sessions.rows.every((r) => r.worktree === null)).toBe(true);
	});
});

describe("row actions", () => {
	it("offers Close worktree before Delete, only on this computer's worktree rows", () => {
		const ids = rowActions(false, false, false, true).map((a) => a.id);
		expect(ids.indexOf("close-worktree")).toBe(ids.indexOf("delete") - 1);
		expect(rowActions(false).map((a) => a.id)).not.toContain("close-worktree");
		expect(rowActions(false, false, true, true).map((a) => a.id)).not.toContain("close-worktree");
		expect(CLOSE_WORKTREE_ACTION.danger).toBe(false);
		// Move waits until the worktree is closed.
		expect(ids).not.toContain("move");
		expect(rowActions(false).map((a) => a.id)).toContain("move");
	});
});

describe("the rail row", () => {
	async function mountRow(): Promise<void> {
		emit(list(WT));
		app = mount(RailSessionRow, {
			target: document.body,
			props: { row: sessions.rows[0], active: false, onselect: () => {} },
		});
		await tick();
	}

	it("shows the branch", async () => {
		await mountRow();
		expect(document.body.textContent).toContain("tst/wt/1a2b3c4d");
	});

	it("closes an unchanged worktree in one step", async () => {
		await mountRow();
		sessions.menuFor = "sess-1";
		await tick();
		button("Close worktree…").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "close_worktree", session_id: "sess-1" });
		expect(document.body.textContent).toContain("Checking the worktree…");
		emit(list(null));
		await tick();
		expect(sessions.closeWorktreeFor).toBeNull();
	});

	it("asks keep, merge back or remove when the worktree has changes", async () => {
		await mountRow();
		sessions.menuFor = "sess-1";
		await tick();
		button("Close worktree…").click();
		await tick();
		emit({
			type: "error",
			seq: 1,
			session_id: "sess-1",
			code: "worktree_has_changes",
			message: "The worktree on tst/wt/1a2b3c4d has 2 changed files. Keep it, merge it back, or remove it.",
		});
		await tick();
		expect(document.body.textContent).toContain("2 changed files");
		expect(hasButton("Keep") && hasButton("Merge back") && hasButton("Remove")).toBe(true);

		button("Merge back").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "close_worktree", session_id: "sess-1", action: "merge" });

		// A refused merge lands on the row, where the click was.
		emit({
			type: "error",
			seq: 1,
			session_id: "sess-1",
			code: "worktree_failed",
			message: "tst/wt/1a2b3c4d would conflict with the main checkout.",
		});
		await tick();
		expect(document.body.textContent).toContain("would conflict");

		button("Cancel").click();
		await tick();
		expect(sessions.closeWorktreeFor).toBeNull();
		expect(hasButton("Merge back")).toBe(false);
	});
});

describe("the start screen", () => {
	it("words the worktree, the dirty checkout and the setup", () => {
		expect(worktreeNotice(WT)).toEqual({
			where: "Works in its own git worktree on tst/wt/1a2b3c4d.",
			dirty: null,
			setup: null,
			setupWarn: false,
		});
		const dirty = worktreeNotice({ ...WT, main_dirty: 1, setup: "running" });
		expect(dirty.dirty).toContain("1 uncommitted change.");
		expect(dirty.dirty).toContain("won't have it.");
		expect(dirty.setup).toContain("first message waits");
		const failed = worktreeNotice({ ...WT, setup: "failed", setup_note: "exit 1" });
		expect(failed.setup).toBe("Worktree setup failed. exit 1");
		expect(failed.setupWarn).toBe(true);
	});

	it("offers the main checkout instead when the checkout had changes", async () => {
		app = mount(WorktreeNotice, {
			target: document.body,
			props: { sessionId: "sess-1", worktree: { ...WT, main_dirty: 3 } },
		});
		await tick();
		expect(document.body.textContent).toContain("3 uncommitted changes");
		button("Start in the main checkout instead").click();
		expect(mocks.sent.at(-1)).toEqual({ type: "close_worktree", session_id: "sess-1", action: "remove" });
	});

	it("waits for the setup before it offers the main checkout", async () => {
		app = mount(WorktreeNotice, {
			target: document.body,
			props: { sessionId: "sess-1", worktree: { ...WT, main_dirty: 1, setup: "running" } },
		});
		await tick();
		expect(button("Start in the main checkout instead").disabled).toBe(true);
		expect(document.body.textContent).toContain("Setting up the worktree");
	});

	it("offers nothing to undo when the checkout was clean", async () => {
		app = mount(WorktreeNotice, {
			target: document.body,
			props: { sessionId: "sess-1", worktree: WT },
		});
		await tick();
		expect(hasButton("Start in the main checkout instead")).toBe(false);
		expect(document.body.textContent).toContain("tst/wt/1a2b3c4d");
		closeRowMenus();
	});
});
