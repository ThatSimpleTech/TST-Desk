// Greeting boundary tests (TD-1605): the morning/afternoon/evening splits
// and the chip copy are the contract ChatPane renders.

import { describe, expect, it } from "vitest";
import { greetingContext, greetingForHour, SUGGESTIONS } from "./greeting";

describe("greetingForHour", () => {
	it("greets morning from 05:00 through 11:59", () => {
		expect(greetingForHour(5)).toBe("Good morning");
		expect(greetingForHour(11)).toBe("Good morning");
	});

	it("greets afternoon from 12:00 through 16:59", () => {
		expect(greetingForHour(12)).toBe("Good afternoon");
		expect(greetingForHour(16)).toBe("Good afternoon");
	});

	it("greets evening from 17:00 through 04:59", () => {
		expect(greetingForHour(17)).toBe("Good evening");
		expect(greetingForHour(23)).toBe("Good evening");
		expect(greetingForHour(0)).toBe("Good evening");
		expect(greetingForHour(4)).toBe("Good evening");
	});
});

describe("SUGGESTIONS", () => {
	it("are the three product-voice chips", () => {
		expect([...SUGGESTIONS]).toEqual([
			"Review this repo",
			"Find what's failing",
			"Explain this codebase",
		]);
	});
});

describe("greetingContext", () => {
	it("names native engine, tier, and slug for this chat", () => {
		expect(
			greetingContext({
				workspace: "TST-Desk",
				engine: "native",
				tier: "brain",
				slug: "Stealth/ox-alpha",
			}),
		).toBe("TST-Desk · native · brain · Stealth/ox-alpha");
	});

	it("names grok engine and the Grok model, not native slugs", () => {
		expect(
			greetingContext({
				workspace: "TST-Desk",
				engine: "grok",
				tier: "brain",
				slug: "Stealth/ox-alpha",
				grokModel: "grok-4.6",
				grokMode: "plan",
			}),
		).toBe("TST-Desk · grok · grok-4.6");
	});

	it("falls back to grok mode when no model is named yet", () => {
		expect(
			greetingContext({
				workspace: "TST-Desk",
				engine: "grok",
				grokMode: "plan",
			}),
		).toBe("TST-Desk · grok · plan");
	});

	it("omits anything the daemon has not named", () => {
		expect(greetingContext({ workspace: "TST-Desk", engine: "grok" })).toBe(
			"TST-Desk · grok",
		);
		expect(greetingContext({ engine: "native", tier: "brain" })).toBe(
			"native · brain",
		);
		expect(greetingContext({})).toBe("");
	});
});
