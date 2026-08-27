// Tests for the rail's information architecture (TD-1712).
//
// The grouping rules are pure, so they are asserted here rather than through a
// rendered rail. The one that matters most is negative: a surface whose epic
// hasn't landed must never look clickable. That is exactly the kind of rule
// that reads fine in review and regresses silently, so it is pinned from two
// directions — the registry invariant here, the refusing dispatcher in
// sessions.test.ts.

import { describe, it, expect } from "vitest";
import { ICONS } from "./icons";
import {
	RAIL_FUNCTIONS,
	railFunctions,
	railSections,
	historyBadge,
	entryHint,
	accountRow,
	rowActions,
	archivedToggle,
	starredToggle,
	emptyRowsCopy,
	DELETE_CONFIRM,
	MOVE_HINT,
} from "./rail";

describe("rail sections", () => {
	it("groups function entries above session history", () => {
		const [first, second] = railSections(0);
		expect(first.id).toBe("functions");
		expect(second.id).toBe("history");
		expect(railSections(0)).toHaveLength(2);
	});

	it("puts Home, Projects, Artifacts and Scheduled in the function group, in rail order", () => {
		const [functions] = railSections(3);
		expect(functions.entries.map((e) => e.id)).toEqual([
			"home",
			"projects",
			"artifacts",
			"scheduled",
		]);
		expect(functions.entries.map((e) => e.label)).toEqual([
			"Home",
			"Projects",
			"Artifacts",
			"Scheduled",
		]);
	});

	it("heads the history section and leaves the function group unheaded", () => {
		const [functions, history] = railSections(1);
		expect(functions.heading).toBe(false);
		expect(history.heading).toBe(true);
		expect(history.label).toBe("History");
	});

	it("leaves the rows to the store — the history section carries no entries", () => {
		expect(railSections(5)[1].entries).toEqual([]);
	});

	it("badges the history count and shows nothing at zero", () => {
		expect(railSections(0)[1].badge).toBeNull();
		expect(railSections(1)[1].badge).toBe("1");
		expect(railSections(12)[1].badge).toBe("12");
	});

	it("only the history section badges", () => {
		expect(railSections(9)[0].badge).toBeNull();
	});
});

describe("historyBadge", () => {
	it("saturates rather than widening the rail", () => {
		expect(historyBadge(99)).toBe("99");
		expect(historyBadge(100)).toBe("99+");
		expect(historyBadge(4000)).toBe("99+");
	});

	it("treats a negative count as nothing to badge", () => {
		expect(historyBadge(-1)).toBeNull();
	});
});

// ── Never a dead click (AC 3) ─────────────────────────────────────────────

const entry = (id: string) => RAIL_FUNCTIONS.find((e) => e.id === id);

describe("surfaces that can't be clicked", () => {
	it("marks Scheduled ready so the rail can open it (TD-3805)", () => {
		expect(entry("scheduled")?.state).toBe("ready");
	});

	it("does not put Memory on the rail (TD-2601)", () => {
		const ids = railFunctions("home").map((e) => e.id);
		expect(ids).not.toContain("memory");
		expect(ids).toEqual(["home", "projects", "artifacts", "scheduled"]);
		expect(railFunctions("home").every((e) => e.state !== "planned")).toBe(true);
	});

	it("marks Home current rather than a destination — the pane is already it", () => {
		expect(entry("home")?.state).toBe("current");
	});

	it("gives every planned entry a milestone note, and no other entry one", () => {
		for (const e of RAIL_FUNCTIONS) {
			if (e.state === "planned") expect(e.note).not.toBeNull();
			else expect(e.note).toBeNull();
		}
	});

	it("hints why each row can't be clicked, instead of looking broken", () => {
		const scheduled = entry("scheduled");
		const home = entry("home");
		expect(scheduled && entryHint(scheduled)).toBe("Scheduled");
		expect(home && entryHint(home)).toBe("Home — you are here");
	});

	it("hints a ready entry with its plain label", () => {
		const projects = entry("projects");
		expect(projects && entryHint(projects)).toBe("Projects");
		const artifacts = entry("artifacts");
		expect(artifacts?.state).toBe("ready");
		expect(artifacts && entryHint(artifacts)).toBe("Artifacts");
	});

	it("swaps current and ready when the window is on Projects (TD-2801)", () => {
		const entries = railFunctions("projects");
		expect(entries.find((e) => e.id === "projects")?.state).toBe("current");
		expect(entries.find((e) => e.id === "home")?.state).toBe("ready");
		expect(entries.find((e) => e.id === "artifacts")?.state).toBe("ready");
		expect(entries.find((e) => e.id === "scheduled")?.state).toBe("ready");
		expect(entryHint(entries.find((e) => e.id === "projects")!)).toBe("Projects — you are here");
		expect(entryHint(entries.find((e) => e.id === "home")!)).toBe("Home");
	});

	it("marks Artifacts current when that surface is showing (TD-3202)", () => {
		const entries = railFunctions("artifacts");
		expect(entries.find((e) => e.id === "artifacts")?.state).toBe("current");
		expect(entries.find((e) => e.id === "home")?.state).toBe("ready");
		expect(entries.find((e) => e.id === "projects")?.state).toBe("ready");
		expect(entries.find((e) => e.id === "scheduled")?.state).toBe("ready");
		expect(entryHint(entries.find((e) => e.id === "artifacts")!)).toBe(
			"Artifacts — you are here",
		);
	});

	it("marks Scheduled current when that surface is showing (TD-3805)", () => {
		const entries = railFunctions("scheduled");
		expect(entries.find((e) => e.id === "scheduled")?.state).toBe("current");
		expect(entries.find((e) => e.id === "home")?.state).toBe("ready");
		expect(entries.find((e) => e.id === "projects")?.state).toBe("ready");
		expect(entries.find((e) => e.id === "artifacts")?.state).toBe("ready");
		expect(entryHint(entries.find((e) => e.id === "scheduled")!)).toBe(
			"Scheduled — you are here",
		);
	});

	it("names an icon the shared map actually has", () => {
		for (const e of RAIL_FUNCTIONS) expect(ICONS).toHaveProperty(e.icon);
	});
});

// ── Account anchor (AC 2) ─────────────────────────────────────────────────

describe("accountRow", () => {
	it("takes its label and initial from the daemon's active preset", () => {
		expect(accountRow("tst-default", true)).toEqual({
			initial: "T",
			label: "tst-default",
			note: "Key stored",
		});
	});

	it("says so when no preset is set, and leaves the avatar to the glyph", () => {
		const row = accountRow(null, false);
		expect(row.initial).toBeNull();
		expect(row.label).toBe("No provider");
	});

	it("reports key presence, never a key", () => {
		expect(accountRow("budget", false).note).toBe("No key");
		expect(accountRow("budget", true).note).toBe("Key stored");
	});

	it("skips punctuation when picking the initial", () => {
		expect(accountRow("-local", true).initial).toBe("L");
		expect(accountRow("...", true).initial).toBeNull();
	});
});
// ── Row lifecycle affordances (TD-1715) ──────────────────────────────────
//
// The grammar of the row menu, pinned here for the same reason the surface
// registry is: it is pure, and the rules that matter are about what is *not*
// offered.

describe("rowActions", () => {
	it("offers archive, move and delete on a live row", () => {
		expect(rowActions(false).map((a) => a.id)).toEqual([
			"star",
			"rename",
			"archive",
			"move",
			"open-window",
			"delete",
		]);
	});

	it("swaps archive for unarchive on a filed row, never both", () => {
		const ids = rowActions(true).map((a) => a.id);
		expect(ids).toEqual(["star", "rename", "unarchive", "move", "open-window", "delete"]);
		expect(ids).not.toContain("archive");
	});

	it("swaps star for unstar on a starred row, never both", () => {
		expect(rowActions(false, true).map((a) => a.id)).toEqual([
			"unstar",
			"rename",
			"archive",
			"move",
			"open-window",
			"delete",
		]);
		expect(rowActions(false, false).map((a) => a.id)).not.toContain("unstar");
	});

	it("marks only delete destructive", () => {
		for (const archived of [false, true]) {
			const danger = rowActions(archived).filter((a) => a.danger).map((a) => a.id);
			expect(danger).toEqual(["delete"]);
		}
	});

	it("names an icon the shared map actually has", () => {
		for (const archived of [false, true]) {
			for (const a of rowActions(archived)) expect(ICONS).toHaveProperty(a.icon);
		}
	});

	it("gives every action hover copy that says what it costs", () => {
		for (const archived of [false, true]) {
			for (const a of rowActions(archived)) expect(a.hint.length).toBeGreaterThan(0);
		}
	});
});

describe("lifecycle copy", () => {
	// The backlog is explicit that Move must say the working context moves — a
	// session silently running against the wrong root is the failure this copy
	// exists to prevent.
	it("says plainly that moving changes the agent's working context", () => {
		expect(MOVE_HINT).toMatch(/working directory/i);
		expect(MOVE_HINT).toMatch(/boundary root/i);
		expect(MOVE_HINT).toMatch(/next turn/i);
	});

	it("delete's confirm names what is destroyed and offers archive instead", () => {
		expect(DELETE_CONFIRM).toMatch(/event log/i);
		expect(DELETE_CONFIRM).toMatch(/archive/i);
	});

	it("move's hint is the same sentence wherever Move is offered", () => {
		const move = rowActions(false).find((a) => a.id === "move");
		expect(move?.hint).toBe(MOVE_HINT);
	});
});

describe("archived shelf", () => {
	it("renames the history heading rather than adding a second section", () => {
		expect(railSections(2, false)[1].label).toBe("History");
		expect(railSections(2, true)[1].label).toBe("Archived");
		expect(railSections(2, true)).toHaveLength(2);
	});

	it("badges the archived shelf with its own count", () => {
		expect(railSections(0, true)[1].badge).toBeNull();
		expect(railSections(7, true)[1].badge).toBe("7");
	});

	it("labels the toggle with where the click goes, not where you are", () => {
		expect(archivedToggle(false).label).toBe("Archived");
		expect(archivedToggle(true).label).toBe("Sessions");
	});

	it("labels the starred filter with where the click goes", () => {
		expect(starredToggle(false).label).toBe("Starred");
		expect(starredToggle(true).label).toBe("All");
	});
});

describe("emptyRowsCopy", () => {
	it("tells an empty shelf apart from an empty filter", () => {
		expect(emptyRowsCopy(false, false, false)).toBe("No sessions yet");
		expect(emptyRowsCopy(true, false, false)).toBe("No archived sessions");
		expect(emptyRowsCopy(false, true, true)).toBe("No matching sessions");
		expect(emptyRowsCopy(true, true, true)).toBe("No matching sessions");
		expect(emptyRowsCopy(false, false, false, true)).toBe("No starred sessions");
	});

	it("does not claim a filter hid rows when the shelf itself is empty", () => {
		expect(emptyRowsCopy(true, true, false)).toBe("No archived sessions");
	});
});
