// Tests for the project list / home (TD-2801).

import { describe, it, expect, beforeEach } from "vitest";
import {
	projectListEmptyCopy,
	projectRecentsEmptyCopy,
	pinnedProjects,
	projectSessions,
	unpinnedRecents,
} from "./projects";
import {
	projects,
	resetProjects,
	selectProject,
	showHome,
	showProjects,
} from "./projects.svelte.js";

describe("projectSessions", () => {
	const rows = [
		{ sessionId: "a", workspacePath: "/ws/one", archived: false, updatedAt: "2026-08-20T10:00:00Z" },
		{ sessionId: "b", workspacePath: "/ws/two", archived: false, updatedAt: "2026-08-20T12:00:00Z" },
		{ sessionId: "c", workspacePath: "/ws/one", archived: true, updatedAt: "2026-08-20T13:00:00Z" },
		{ sessionId: "d", workspacePath: "/ws/one", archived: false, updatedAt: "2026-08-20T11:00:00Z" },
	];

	it("keeps this workspace, newest first, drops other folders and archived rows", () => {
		expect(projectSessions(rows, "/ws/one").map((r) => r.sessionId)).toEqual(["d", "a"]);
	});

	it("shows archived rows only when the Archived filter is on", () => {
		expect(projectSessions(rows, "/ws/one", { archived: true }).map((r) => r.sessionId)).toEqual([
			"c",
		]);
	});

	it("returns nothing for a folder with no live sessions", () => {
		expect(projectSessions(rows, "/ws/none")).toEqual([]);
	});
});

describe("workspace pins", () => {
	const entries = [
		{ path: "/ws/one", lastSeen: "2" },
		{ path: "/ws/two", lastSeen: "1" },
	];

	it("keeps pinned paths even when they aged out of recents", () => {
		expect(pinnedProjects(entries, ["/ws/old", "/ws/one"]).map((e) => e.path)).toEqual([
			"/ws/old",
			"/ws/one",
		]);
	});

	it("leaves unpinned known workspaces under Recents", () => {
		expect(unpinnedRecents(entries, ["/ws/one"]).map((e) => e.path)).toEqual(["/ws/two"]);
	});
});

describe("copy", () => {
	it("points an empty list at the title-bar picker", () => {
		expect(projectListEmptyCopy()).toMatch(/title bar/i);
	});

	it("does not invent chats on an empty home", () => {
		expect(projectRecentsEmptyCopy()).toMatch(/no chats/i);
	});

	it("names the archived shelf when that filter is on", () => {
		expect(projectRecentsEmptyCopy(true)).toMatch(/archived/i);
	});
});

describe("surface store", () => {
	beforeEach(() => {
		resetProjects();
	});

	it("starts on home with no project selected", () => {
		expect(projects.surface).toBe("home");
		expect(projects.selectedPath).toBeNull();
	});

	it("Projects is the list — selecting a project is a second step", () => {
		showProjects();
		expect(projects.surface).toBe("projects");
		expect(projects.selectedPath).toBeNull();
		selectProject("/ws/desk");
		expect(projects.selectedPath).toBe("/ws/desk");
		showProjects();
		expect(projects.selectedPath).toBeNull();
	});

	it("Home leaves the selected path so Back can reopen the same home", () => {
		selectProject("/ws/desk");
		showHome();
		expect(projects.surface).toBe("home");
		expect(projects.selectedPath).toBe("/ws/desk");
	});
});
