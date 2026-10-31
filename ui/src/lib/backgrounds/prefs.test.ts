// The per-device background preference (TD-5202): what a fresh device
// gets, what a stored value reads back as, what a broken one falls back
// to, and that storage failing never throws. Strength is Subtle unless the
// device chose Vivid.

import { describe, expect, it } from "vitest";
import { BACKGROUND_IDS, BACKGROUNDS, DEFAULT_BACKGROUND, backgroundInfo, isBackgroundId } from "./catalog";
import {
	BACKGROUND_KEY,
	DEFAULT_PREFS,
	LAST_ON_KEY,
	MOTION_KEY,
	RIPPLES_KEY,
	STRENGTHS,
	STRENGTH_KEY,
	animates,
	flagValue,
	isStrength,
	readLastOn,
	readPrefs,
	ripplesLive,
	writeItem,
} from "./prefs";

function memoryStore(seed: Record<string, string> = {}): Storage {
	const data = new Map(Object.entries(seed));
	return {
		get length() {
			return data.size;
		},
		clear: () => data.clear(),
		getItem: (key) => data.get(key) ?? null,
		key: (i) => [...data.keys()][i] ?? null,
		removeItem: (key) => void data.delete(key),
		setItem: (key, value) => void data.set(key, String(value)),
	};
}

const throwing: Storage = {
	length: 0,
	clear() {
		throw new Error("blocked");
	},
	getItem() {
		throw new Error("blocked");
	},
	key() {
		throw new Error("blocked");
	},
	removeItem() {
		throw new Error("blocked");
	},
	setItem() {
		throw new Error("QuotaExceededError");
	},
};

describe("catalog", () => {
	it("keeps Classic as the default so nobody's window changes on upgrade", () => {
		expect(DEFAULT_BACKGROUND).toBe("classic");
		expect(DEFAULT_PREFS).toEqual({ id: "classic", motion: true, ripples: true, strength: "subtle" });
	});

	it("offers Off, Classic, Galaxy, Deep space and at least two more, each named once", () => {
		expect(BACKGROUND_IDS.slice(0, 4)).toEqual(["off", "classic", "galaxy", "deep-space"]);
		expect(BACKGROUND_IDS.length).toBeGreaterThanOrEqual(6);
		expect(BACKGROUNDS.map((b) => b.id)).toEqual([...BACKGROUND_IDS]);
		expect(new Set(BACKGROUNDS.map((b) => b.label)).size).toBe(BACKGROUNDS.length);
		for (const b of BACKGROUNDS) expect(b.blurb.length).toBeGreaterThan(10);
	});

	it("knows its own ids and nothing else", () => {
		expect(isBackgroundId("galaxy")).toBe(true);
		expect(isBackgroundId("Galaxy")).toBe(false);
		expect(isBackgroundId("")).toBe(false);
		expect(isBackgroundId(null)).toBe(false);
		expect(backgroundInfo("aurora").label).toBe("Aurora");
	});
});

describe("readPrefs", () => {
	it("gives the defaults on a fresh device", () => {
		expect(readPrefs(memoryStore())).toEqual(DEFAULT_PREFS);
	});

	it("gives the defaults when there is no storage at all", () => {
		expect(readPrefs(null)).toEqual(DEFAULT_PREFS);
	});

	it("reads back what was stored", () => {
		const store = memoryStore({
			[BACKGROUND_KEY]: "galaxy",
			[MOTION_KEY]: "off",
			[RIPPLES_KEY]: "off",
			[STRENGTH_KEY]: "vivid",
		});
		expect(readPrefs(store)).toEqual({ id: "galaxy", motion: false, ripples: false, strength: "vivid" });
	});

	it("falls back field by field: a bad background keeps good flags", () => {
		const store = memoryStore({ [BACKGROUND_KEY]: "nebula-9000", [MOTION_KEY]: "off" });
		expect(readPrefs(store)).toEqual({ id: "classic", motion: false, ripples: true, strength: "subtle" });
	});

	it("reads a strength other than subtle or vivid as Subtle, keeping the rest", () => {
		for (const junk of ["Vivid", "VIVID", "strong", "", "0.4", '"vivid"']) {
			const store = memoryStore({ [STRENGTH_KEY]: junk, [BACKGROUND_KEY]: "aurora" });
			expect(readPrefs(store), junk).toEqual({ ...DEFAULT_PREFS, id: "aurora" });
		}
		expect(STRENGTHS).toEqual(["subtle", "vivid"]);
		expect(isStrength("vivid")).toBe(true);
		expect(isStrength(null)).toBe(false);
	});

	it("treats anything but on/off as unset", () => {
		for (const junk of ["true", "1", "", "ON", '{"on":true}']) {
			const store = memoryStore({
				[MOTION_KEY]: junk,
				[RIPPLES_KEY]: junk,
				[BACKGROUND_KEY]: junk,
				[STRENGTH_KEY]: junk,
			});
			expect(readPrefs(store)).toEqual(DEFAULT_PREFS);
		}
	});

	it("reads storage that throws as empty, without throwing", () => {
		expect(readPrefs(throwing)).toEqual(DEFAULT_PREFS);
	});
});

describe("readLastOn", () => {
	it("reads the last background that was on, never Off, Classic by default", () => {
		expect(readLastOn(memoryStore({ [LAST_ON_KEY]: "fireflies" }))).toBe("fireflies");
		expect(readLastOn(memoryStore({ [LAST_ON_KEY]: "off" }))).toBe("classic");
		expect(readLastOn(memoryStore({ [LAST_ON_KEY]: "junk" }))).toBe("classic");
		expect(readLastOn(throwing)).toBe("classic");
		expect(readLastOn(null)).toBe("classic");
	});
});

describe("writeItem", () => {
	it("stores a value and says so", () => {
		const store = memoryStore();
		expect(writeItem(store, BACKGROUND_KEY, "aurora")).toBe(true);
		expect(store.getItem(BACKGROUND_KEY)).toBe("aurora");
	});

	it("reports a write that could not be kept instead of throwing", () => {
		expect(writeItem(throwing, BACKGROUND_KEY, "aurora")).toBe(false);
		expect(writeItem(null, BACKGROUND_KEY, "aurora")).toBe(false);
	});

	it("round-trips flags", () => {
		const store = memoryStore();
		writeItem(store, MOTION_KEY, flagValue(false));
		writeItem(store, RIPPLES_KEY, flagValue(true));
		expect(readPrefs(store)).toMatchObject({ motion: false, ripples: true });
	});

	it("round-trips a strength, and a refused write keeps Subtle on the next read", () => {
		const store = memoryStore();
		expect(writeItem(store, STRENGTH_KEY, "vivid")).toBe(true);
		expect(readPrefs(store).strength).toBe("vivid");
		expect(writeItem(throwing, STRENGTH_KEY, "vivid")).toBe(false);
		expect(readPrefs(throwing).strength).toBe("subtle");
	});
});

describe("what the window does", () => {
	const base = { id: "galaxy" as const, motion: true, ripples: true, strength: "subtle" as const, reducedMotion: false };

	it("animates and ripples by default", () => {
		expect(animates(base)).toBe(true);
		expect(ripplesLive(base)).toBe(true);
	});

	it("Off never animates and never ripples, whatever the toggles say", () => {
		expect(animates({ ...base, id: "off" })).toBe(false);
		expect(ripplesLive({ ...base, id: "off" })).toBe(false);
	});

	it("Motion off holds still; ripples are their own switch", () => {
		expect(animates({ ...base, motion: false })).toBe(false);
		expect(ripplesLive({ ...base, motion: false })).toBe(true);
		expect(ripplesLive({ ...base, ripples: false })).toBe(false);
	});

	it("the OS asking for reduced motion means a still frame and no ripples", () => {
		expect(animates({ ...base, reducedMotion: true })).toBe(false);
		expect(ripplesLive({ ...base, reducedMotion: true })).toBe(false);
	});
});
