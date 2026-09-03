// Command palette registry and matcher (TD-1707).
//
// Two things are worth pinning here. The matcher has to behave the way a
// palette user assumes — an abbreviation finds its command, a title hit
// outranks a body hit, and a query that isn't a subsequence finds nothing —
// and every entry has to name a glyph the shared icon map actually has, since
// a typo there ships silently as a blank square instead of failing.

import { describe, it, expect } from "vitest";
import { ICONS } from "./icons";
import {
	ACTION_ENTRIES,
	fuzzyScore,
	matchScore,
	nextIndex,
	rankEntries,
	type PaletteEntry,
} from "./palette";

function titles(entries: readonly PaletteEntry[], query: string): string[] {
	return rankEntries(entries, query).map((e) => e.title);
}

describe("fuzzyScore", () => {
	it("matches a subsequence, not just a substring", () => {
		expect(fuzzyScore("opdec", "Open decisions")).not.toBeNull();
		expect(fuzzyScore("nse", "New session")).not.toBeNull();
	});

	it("refuses a query whose characters aren't in order", () => {
		expect(fuzzyScore("zzz", "Open settings")).toBeNull();
		expect(fuzzyScore("snoitces", "sections")).toBeNull();
	});

	it("ignores case in both directions", () => {
		expect(fuzzyScore("DOC", "Run doctor")).not.toBeNull();
		expect(fuzzyScore("run", "RUN DOCTOR")).not.toBeNull();
	});

	it("matches everything on an empty query", () => {
		expect(fuzzyScore("", "anything")).toBe(0);
	});

	it("scores a word-start hit above a mid-word hit", () => {
		const front = fuzzyScore("set", "Settings pane");
		const middle = fuzzyScore("set", "Unset flag");
		expect(front).not.toBeNull();
		expect(middle).not.toBeNull();
		expect(front as number).toBeGreaterThan(middle as number);
	});

	it("scores a tight run above a scattered one", () => {
		const tight = fuzzyScore("stack", "stackpanel") as number;
		const scattered = fuzzyScore("stack", "sxtxaxcxk") as number;
		expect(tight).toBeGreaterThan(scattered);
	});
});

describe("matchScore", () => {
	const entry: PaletteEntry = {
		id: "x",
		title: "Open stack",
		subtitle: "The resolved instruction stack",
		icon: "layers",
		keywords: "steering context",
		command: { kind: "show-stack" },
	};

	it("searches the subtitle and the keywords, not only the title", () => {
		expect(matchScore("steering", entry)).not.toBeNull();
		expect(matchScore("resolved", entry)).not.toBeNull();
	});

	it("ranks a title hit above a body-only hit", () => {
		const bodyOnly: PaletteEntry = { ...entry, title: "Zzz", id: "y" };
		const titleHit = matchScore("stack", entry) as number;
		const bodyHit = matchScore("stack", bodyOnly) as number;
		expect(titleHit).toBeGreaterThan(bodyHit);
	});
});

describe("rankEntries", () => {
	it("keeps registry order on an empty query", () => {
		expect(titles(ACTION_ENTRIES, "")).toEqual(ACTION_ENTRIES.map((e) => e.title));
		expect(titles(ACTION_ENTRIES, "   ")).toEqual(ACTION_ENTRIES.map((e) => e.title));
	});

	it("puts the obvious answer first for the abbreviations people type", () => {
		expect(titles(ACTION_ENTRIES, "doc")[0]).toBe("Run doctor");
		expect(titles(ACTION_ENTRIES, "stack")[0]).toBe("Open stack");
		expect(titles(ACTION_ENTRIES, "work")[0]).toBe("Open work");
		expect(titles(ACTION_ENTRIES, "theme")[0]).toBe("Toggle theme");
		expect(titles(ACTION_ENTRIES, "new")[0]).toBe("New session");
		expect(titles(ACTION_ENTRIES, "sett")[0]).toBe("Open settings");
		expect(titles(ACTION_ENTRIES, "dec")[0]).toBe("Open decisions");
		expect(titles(ACTION_ENTRIES, "quit")[0]).toBe("Quit TST Desk");
		expect(titles(ACTION_ENTRIES, "stop cu")[0]).toBe("Stop computer use");
		expect(titles(ACTION_ENTRIES, "resume")[0]).toBe("Resume computer use");
		expect(titles(ACTION_ENTRIES, "design")[0]).toBe("Toggle Design mode");
	});

	it("finds an entry by a word only its keywords carry", () => {
		expect(titles(ACTION_ENTRIES, "ledger")[0]).toBe("Open decisions");
		expect(titles(ACTION_ENTRIES, "diagnostics")[0]).toBe("Run doctor");
	});

	it("drops the entries that don't match at all", () => {
		const two: PaletteEntry[] = [
			{ ...ACTION_ENTRIES[0], id: "a", title: "Alpha", subtitle: "", keywords: "" },
			{ ...ACTION_ENTRIES[0], id: "b", title: "Beta", subtitle: "", keywords: "" },
		];
		expect(rankEntries(two, "alp").map((e) => e.id)).toEqual(["a"]);
		expect(rankEntries(ACTION_ENTRIES, "qqqq")).toEqual([]);
	});

	it("breaks ties by registry order rather than shuffling", () => {
		const tied: PaletteEntry[] = [
			{ ...ACTION_ENTRIES[0], id: "a", title: "Same name" },
			{ ...ACTION_ENTRIES[1], id: "b", title: "Same name" },
		];
		expect(rankEntries(tied, "same").map((e) => e.id)).toEqual(["a", "b"]);
	});
});

describe("nextIndex", () => {
	it("wraps in both directions", () => {
		expect(nextIndex(0, 1, 3)).toBe(1);
		expect(nextIndex(2, 1, 3)).toBe(0);
		expect(nextIndex(0, -1, 3)).toBe(2);
	});

	it("stays at 0 on an empty list", () => {
		expect(nextIndex(0, 1, 0)).toBe(0);
		expect(nextIndex(0, -1, 0)).toBe(0);
	});
});

describe("icons", () => {
	it("names only glyphs the shared map has", () => {
		// The type already says IconName, but a cast or a widened literal
		// anywhere upstream would land here as a blank square at runtime.
		for (const entry of ACTION_ENTRIES) {
			expect(Object.keys(ICONS)).toContain(entry.icon);
		}
	});

	it("covers every action the palette offers", () => {
		expect(ACTION_ENTRIES.map((e) => e.command.kind).sort()).toEqual([
			"end-session",
			"new-session",
			"new-window",
			"open-decisions",
			"open-in-terminal",
			"open-settings",
			"quit-app",
			"resume-computer-use",
			"run-doctor",
			"show-plan",
			"show-preview",
			"show-stack",
			"show-work",
			"stop-computer-use",
			"toggle-design",
			"toggle-theme",
		]);
	});
});
