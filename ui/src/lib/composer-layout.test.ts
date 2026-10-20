import { describe, expect, it } from "vitest";

import { tierMenuLabel } from "./composer-layout";

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
