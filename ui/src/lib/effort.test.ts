import { describe, expect, it } from "vitest";

import {
	chipLabel,
	DEFAULT_EFFORT,
	EFFORT_OPTIONS,
	groupModels,
	isMoreModel,
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
	it("keeps Ezer on the main list and the raw routes under More models", () => {
		const groups = groupModels([
			"ezer",
			"tst-default",
			"sub-claude-opus",
			"sub-claude-sonnet",
			"sub-grok",
			"sub-cursor",
			"ezer-forge",
		]);
		expect(groups.main).toEqual(["ezer", "tst-default"]);
		expect(groups.more).toEqual([
			"sub-claude-opus",
			"sub-claude-sonnet",
			"sub-grok",
			"sub-cursor",
			"ezer-forge",
		]);
	});

	it("still shows a current model the daemon did not list", () => {
		expect(groupModels(["ezer"], "sub-grok").more).toEqual(["sub-grok"]);
	});

	it("tells routes from models", () => {
		expect(isMoreModel("ezer")).toBe(false);
		expect(isMoreModel("ezer-coder")).toBe(true);
		expect(isMoreModel("sub-grok")).toBe(true);
	});
});

describe("chip label", () => {
	it("reads Model then Effort", () => {
		expect(modelLabel("ezer")).toBe("Ezer");
		expect(chipLabel("ezer", "medium")).toBe("Ezer Medium");
		expect(chipLabel("ezer", "xhigh")).toBe("Ezer Extra");
		expect(chipLabel("ezer", "ultracode")).toBe("Ezer Ultracode");
		expect(chipLabel("budget", "low")).toBe("budget Low");
	});
});
