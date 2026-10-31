import { describe, expect, it } from "vitest";

import { keyboardInsetPx, tierMenuLabel } from "./composer-layout";

describe("keyboardInsetPx", () => {
	it("is the part of the layout viewport the keyboard covers", () => {
		expect(keyboardInsetPx(800, 500)).toBe(300);
		expect(keyboardInsetPx(800, 800)).toBe(0);
	});

	it("is zero when the visual viewport is pinch-zoomed", () => {
		// A zoomed visual viewport reports a smaller height with the keyboard
		// down; using that as padding would lift the composer for nothing.
		expect(keyboardInsetPx(800, 600, 1.5)).toBe(0);
		expect(keyboardInsetPx(800, 500, 0.5)).toBe(0);
	});

	it("never goes negative or invent a height from bad input", () => {
		expect(keyboardInsetPx(800, 900)).toBe(0);
		expect(keyboardInsetPx(0, 500)).toBe(0);
		expect(keyboardInsetPx(800, 0)).toBe(0);
	});
});

describe("tierMenuLabel", () => {
	it("puts the model the tier is running in parentheses", () => {
		expect(tierMenuLabel("brain", "moonshotai/kimi-k3")).toBe("brain (moonshotai/kimi-k3)");
	});

	it("is just the role until a model is known", () => {
		expect(tierMenuLabel("worker", null)).toBe("worker");
		expect(tierMenuLabel("worker", "  ")).toBe("worker");
		expect(tierMenuLabel("validator", undefined)).toBe("validator");
	});

	it("says when a tier is on a backup endpoint this turn (TD-5120)", () => {
		expect(tierMenuLabel("brain", "backup/model", 1)).toBe("brain (backup/model, backup 1)");
		expect(tierMenuLabel("brain", "own/model", 0)).toBe("brain (own/model)");
		expect(tierMenuLabel("brain", null, 2)).toBe("brain (backup 2)");
	});
});
