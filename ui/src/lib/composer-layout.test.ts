import { describe, expect, it } from "vitest";

import { inlineDraftWidth, modelControlsInFooter, tierMenuLabel } from "./composer-layout";

describe("tierMenuLabel", () => {
	it("puts the model the tier is running in parentheses", () => {
		expect(tierMenuLabel("brain", "moonshotai/kimi-k3")).toBe("brain (moonshotai/kimi-k3)");
	});

	it("is just the role until a model is known", () => {
		expect(tierMenuLabel("worker", null)).toBe("worker");
		expect(tierMenuLabel("worker", "  ")).toBe("worker");
		expect(tierMenuLabel("validator", undefined)).toBe("validator");
	});
});

describe("inlineDraftWidth", () => {
	it("reserves the send cluster and the model controls, plus both gaps", () => {
		expect(inlineDraftWidth(760, 80, 200, 8)).toBe(464);
	});

	it("is zero until the row has been laid out", () => {
		expect(inlineDraftWidth(0, 80, 200, 8)).toBe(0);
	});

	it("does not go negative when the controls are wider than the row", () => {
		expect(inlineDraftWidth(200, 180, 160, 8)).toBe(0);
	});
});

describe("modelControlsInFooter", () => {
	it("drops to the footer as soon as the draft has a second line", () => {
		expect(modelControlsInFooter("hello\n", 0, 24, 0)).toBe(true);
	});

	it("stays beside Send while the draft is one line", () => {
		expect(modelControlsInFooter("hello", 24, 24, 200)).toBe(false);
	});

	it("drops to the footer when that same line would wrap beside the controls", () => {
		expect(modelControlsInFooter("hello", 48, 24, 200)).toBe(true);
	});

	it("does not treat an unmeasured box as wrapped", () => {
		expect(modelControlsInFooter("hello", 48, 24, 0)).toBe(false);
	});

	it("drops to the footer when the laid-out row has no room beside the controls", () => {
		expect(modelControlsInFooter("hi", 24, 24, 0, 320)).toBe(true);
	});

	it("ignores a single pixel of rounding", () => {
		expect(modelControlsInFooter("hello", 25, 24, 200)).toBe(false);
		expect(modelControlsInFooter("hello", 26, 24, 200)).toBe(true);
	});
});
