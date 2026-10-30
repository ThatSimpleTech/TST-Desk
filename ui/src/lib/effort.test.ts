import { describe, expect, it } from "vitest";

import {
	chipLabel,
	DEFAULT_EFFORT,
	EFFORT_OPTIONS,
	groupModels,
	modelDescription,
	modelLabel,
} from "./effort";

describe("effort levels", () => {
	it("lists Low, Medium, High, Extra and Max; Ultracode is a switch, not a level (TD-5195)", () => {
		expect(EFFORT_OPTIONS.map((o) => o.label)).toEqual(["Low", "Medium", "High", "Extra", "Max"]);
	});

	it("sends the wire names, Extra being xhigh", () => {
		expect(EFFORT_OPTIONS.map((o) => o.id)).toEqual([
			"low",
			"medium",
			"high",
			"xhigh",
			"max",
		]);
	});

	it("defaults to Medium, badged Recommended; Max warns", () => {
		expect(DEFAULT_EFFORT).toBe("medium");
		expect(EFFORT_OPTIONS.find((o) => o.id === "medium")?.badge).toBe("Recommended");
		expect(EFFORT_OPTIONS.find((o) => o.id === "max")?.warning).toBe("slower, uses the big model");
	});
});

describe("model grouping", () => {
	it("splits by the config's picker flag, main list first", () => {
		const groups = groupModels(["house", "tst-default", "sub-claude-opus", "sub-grok", "house-raw"], {
			more: ["sub-claude-opus", "sub-grok", "house-raw"],
		});
		expect(groups.main).toEqual(["house", "tst-default"]);
		expect(groups.more).toEqual(["sub-claude-opus", "sub-grok", "house-raw"]);
		expect(groups.teams).toEqual([]);
	});

	it("puts every preset on the main list when none says more", () => {
		expect(groupModels(["a", "b"]).main).toEqual(["a", "b"]);
		expect(groupModels(["a", "b"]).more).toEqual([]);
	});

	it("still shows a current model the daemon did not list", () => {
		expect(groupModels(["house"], { more: ["sub-grok"], current: "sub-grok" }).more).toEqual([
			"sub-grok",
		]);
	});

	it("hides presets with no label, except the current one", () => {
		const labels = { house: "House", "sub-grok": "Grok" };
		const groups = groupModels(["house", "raw", "old", "sub-grok"], {
			more: ["sub-grok", "raw"],
			labels,
			current: "old",
		});
		expect(groups.main).toEqual(["house", "old"]);
		expect(groups.more).toEqual(["sub-grok"]);
	});

	it("lists teams only under Teams", () => {
		const groups = groupModels(["house", "crew"], {
			teams: ["crew"],
			labels: { house: "House", crew: "Crew" },
		});
		expect(groups.main).toEqual(["house"]);
		expect(groups.more).toEqual([]);
		expect(groups.teams).toEqual(["crew"]);
	});

	it("an empty catalog lists nothing", () => {
		expect(groupModels([], { labels: {} })).toEqual({ main: [], more: [], teams: [] });
	});

	it("reads a description, or none", () => {
		expect(modelDescription("house", { house: "Coding and chat" })).toBe("Coding and chat");
		expect(modelDescription("raw", {})).toBe("");
	});
});

describe("chip label", () => {
	it("reads Model then Effort, using the configured label", () => {
		const labels = { house: "House" };
		expect(modelLabel("house", labels)).toBe("House");
		expect(modelLabel("budget", labels)).toBe("budget");
		expect(chipLabel("house", "medium", labels)).toBe("House Medium");
		expect(chipLabel("house", "xhigh", labels)).toBe("House Extra");
		expect(chipLabel("house", "max", labels)).toBe("House Max");
		expect(chipLabel("budget", "low")).toBe("budget Low");
	});
});
