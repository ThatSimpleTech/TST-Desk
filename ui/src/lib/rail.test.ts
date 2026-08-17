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
	railSections,
	historyBadge,
	entryHint,
	accountRow,
} from "./rail";

describe("rail sections", () => {
	it("groups function entries above session history", () => {
		const [first, second] = railSections(0);
		expect(first.id).toBe("functions");
		expect(second.id).toBe("history");
		expect(railSections(0)).toHaveLength(2);
	});

	it("puts Home, Projects and Scheduled in the function group, in rail order", () => {
		const [functions] = railSections(3);
		expect(functions.entries.map((e) => e.id)).toEqual(["home", "projects", "scheduled"]);
		expect(functions.entries.map((e) => e.label)).toEqual(["Home", "Projects", "Scheduled"]);
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
	it("marks Scheduled planned, so the rail renders it disabled", () => {
		expect(entry("scheduled")?.state).toBe("planned");
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
		expect(scheduled && entryHint(scheduled)).toBe("Scheduled — arrives in v0.5");
		expect(home && entryHint(home)).toBe("Home — you are here");
	});

	it("hints a ready entry with its plain label", () => {
		const projects = entry("projects");
		expect(projects && entryHint(projects)).toBe("Projects");
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
