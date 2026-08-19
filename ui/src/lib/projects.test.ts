// Tests for the project list / home (TD-2801).

import { describe, it, expect, beforeEach } from "vitest";
import {
	projectListEmptyCopy,
	projectRecentsEmptyCopy,
	projectSessions,
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
		{ sessionId: "a", workspacePath: "/ws/one", archived: false },
		{ sessionId: "b", workspacePath: "/ws/two", archived: false },
		{ sessionId: "c", workspacePath: "/ws/one", archived: true },
		{ sessionId: "d", workspacePath: "/ws/one", archived: false },
	];

	it("keeps this workspace, drops other folders and archived rows", () => {
		expect(projectSessions(rows, "/ws/one").map((r) => r.sessionId)).toEqual(["a", "d"]);
	});

	it("returns nothing for a folder with no live sessions", () => {
		expect(projectSessions(rows, "/ws/none")).toEqual([]);
	});
});

describe("copy", () => {
	it("points an empty list at the title-bar picker", () => {
		expect(projectListEmptyCopy()).toMatch(/title bar/i);
	});

	it("does not invent chats on an empty home", () => {
		expect(projectRecentsEmptyCopy()).toMatch(/no chats/i);
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
