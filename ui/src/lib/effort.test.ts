import { describe, expect, it } from "vitest";

import {
	chipLabel,
	DEFAULT_EFFORT,
	EFFORT_OPTIONS,
	groupModels,
	modelLabel,
} from "./effort";

describe("effort levels", () => {
	it("lists Low, Medium, High, Extra, Max, then Ultracode", () => {
		expect(EFFORT_OPTIONS.map((o) => o.label)).toEqual([
			"Low",
			"Medium",
			"High",
			"Extra",
			"Max",
			"Ultracode",
		]);
	});

	it("sends the wire names, Extra being xhigh", () => {
		expect(EFFORT_OPTIONS.map((o) => o.id)).toEqual([
			"low",
			"medium",
			"high",
			"xhigh",
			"max",
			"ultracode",
		]);
	});

	it("defaults to Medium, badged Recommended; Max warns; Ultracode is accent", () => {
		expect(DEFAULT_EFFORT).toBe("medium");
		expect(EFFORT_OPTIONS.find((o) => o.id === "medium")?.badge).toBe("Recommended");
		expect(EFFORT_OPTIONS.find((o) => o.id === "max")?.warning).toBe("slower, uses the big model");
		expect(EFFORT_OPTIONS.filter((o) => o.accent).map((o) => o.id)).toEqual(["ultracode"]);
	});
});

describe("model grouping", () => {
	it("splits by the config's picker flag, main list first", () => {
		const groups = groupModels(
			["house", "tst-default", "sub-claude-opus", "sub-grok", "house-raw"],
			["sub-claude-opus", "sub-grok", "house-raw"],
		);
		expect(groups.main).toEqual(["house", "tst-default"]);
		expect(groups.more).toEqual(["sub-claude-opus", "sub-grok", "house-raw"]);
	});

	it("puts every preset on the main list when none says more", () => {
		expect(groupModels(["a", "b"]).main).toEqual(["a", "b"]);
		expect(groupModels(["a", "b"]).more).toEqual([]);
	});

	it("still shows a current model the daemon did not list", () => {
		expect(groupModels(["house"], ["sub-grok"], "sub-grok").more).toEqual(["sub-grok"]);
	});
});

describe("chip label", () => {
	it("reads Model then Effort, using the configured label", () => {
		const labels = { house: "House" };
		expect(modelLabel("house", labels)).toBe("House");
		expect(modelLabel("budget", labels)).toBe("budget");
		expect(chipLabel("house", "medium", labels)).toBe("House Medium");
		expect(chipLabel("house", "xhigh", labels)).toBe("House Extra");
		expect(chipLabel("house", "ultracode", labels)).toBe("House Ultracode");
		expect(chipLabel("budget", "low")).toBe("budget Low");
	});
});
