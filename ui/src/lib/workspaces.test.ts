// Tests for the recent-workspaces store (TD-1103).
//
// The store derives recents from session_list events over the connection
// fan-out, so the tests mock that seam and drive real session_list events
// through the reducer. openWorkspace is the switch payload — mocked to
// capture the path without touching a daemon.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	opened: [] as string[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (_msg: ClientMessageUnion) => true,
}));

vi.mock("./session-status.svelte.js", () => ({
	openWorkspace: (path: string) => {
		mocks.opened.push(path);
	},
}));

import {
	workspaces,
	startWorkspaces,
	resetWorkspaces,
	visibleRecents,
	openRecent,
	hideRecent,
	toggleWorkspaceMenu,
	closeWorkspaceMenu,
	HIDDEN_STORAGE_KEY,
} from "./workspaces.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function sessionList(entries: Array<[path: string, updatedAt: string]>): DaemonEventUnion {
	return {
		type: "session_list",
		seq: 1,
		sessions: entries.map(([workspace_path, updated_at], i) => ({
			session_id: `s${i}`,
			workspace_path,
			state: "idle",
			created_at: updated_at,
			updated_at,
			event_count: 3,
		})),
	} as DaemonEventUnion;
}

// Minimal localStorage stub (node test env has none).
const memory = new Map<string, string>();
const storageStub: Storage = {
	get length() {
		return memory.size;
	},
	clear: () => memory.clear(),
	getItem: (k: string) => memory.get(k) ?? null,
	key: (i: number) => [...memory.keys()][i] ?? null,
	removeItem: (k: string) => void memory.delete(k),
	setItem: (k: string, v: string) => void memory.set(k, v),
};

beforeEach(() => {
	vi.stubGlobal("localStorage", storageStub);
	memory.clear();
	mocks.opened.length = 0;
	resetWorkspaces();
	startWorkspaces();
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("derive recents from session_list", () => {
	it("collects workspace paths newest first", () => {
		emit(sessionList([
			["/a/old", "2026-08-10T00:00:00Z"],
			["/b/new", "2026-08-12T00:00:00Z"],
		]));
		expect(visibleRecents().map((r) => r.path)).toEqual(["/b/new", "/a/old"]);
	});

	it("dedupes sessions sharing a workspace, keeping the newest update", () => {
		emit(sessionList([
			["/a/proj", "2026-08-10T00:00:00Z"],
			["/a/proj", "2026-08-13T00:00:00Z"],
		]));
		const recents = visibleRecents();
		expect(recents).toHaveLength(1);
		expect(recents[0].lastSeen).toBe("2026-08-13T00:00:00Z");
	});

	it("caps the menu at 12 entries", () => {
		emit(
			sessionList(
				Array.from({ length: 15 }, (_, i) => [`/p/${i}`, `2026-08-${String(i + 1).padStart(2, "0")}T00:00:00Z`] as [string, string]),
			),
		);
		expect(workspaces.entries).toHaveLength(12);
		expect(workspaces.entries[0].path).toBe("/p/14");
	});
});

describe("switch (AC: quick switch)", () => {
	it("switching sends open_workspace and closes the menu", () => {
		emit(sessionList([["/a/proj", "2026-08-10T00:00:00Z"]]));
		workspaces.menuOpen = true;
		openRecent("/a/proj");
		expect(mocks.opened).toEqual(["/a/proj"]);
		expect(workspaces.menuOpen).toBe(false);
	});
});

describe("remove (AC: per-entry removal)", () => {
	it("hides a path and persists the removal", () => {
		emit(sessionList([
			["/a/one", "2026-08-10T00:00:00Z"],
			["/b/two", "2026-08-11T00:00:00Z"],
		]));
		hideRecent("/a/one");
		expect(visibleRecents().map((r) => r.path)).toEqual(["/b/two"]);
		expect(memory.get(HIDDEN_STORAGE_KEY)).toBe('["/a/one"]');
	});

	it("a hidden path stays hidden across later session_list events", () => {
		emit(sessionList([["/a/one", "2026-08-10T00:00:00Z"]]));
		hideRecent("/a/one");
		emit(sessionList([
			["/a/one", "2026-08-13T00:00:00Z"],
			["/b/two", "2026-08-12T00:00:00Z"],
		]));
		expect(visibleRecents().map((r) => r.path)).toEqual(["/b/two"]);
	});

	it("restores the hide-list from storage on start", () => {
		memory.set(HIDDEN_STORAGE_KEY, '["/gone/dir"]');
		resetWorkspaces();
		startWorkspaces();
		emit(sessionList([
			["/gone/dir", "2026-08-10T00:00:00Z"],
			["/here/dir", "2026-08-11T00:00:00Z"],
		]));
		expect(visibleRecents().map((r) => r.path)).toEqual(["/here/dir"]);
	});

	it("corrupt storage is ignored", () => {
		memory.set(HIDDEN_STORAGE_KEY, "{not json");
		resetWorkspaces();
		startWorkspaces();
		expect(workspaces.hidden).toEqual([]);
	});
});

describe("menu open/close", () => {
	it("toggles and closes", () => {
		expect(workspaces.menuOpen).toBe(false);
		toggleWorkspaceMenu();
		expect(workspaces.menuOpen).toBe(true);
		closeWorkspaceMenu();
		expect(workspaces.menuOpen).toBe(false);
	});
});
