// Greeting boundary tests (TD-1605): the morning/afternoon/evening splits
// and the chip copy are the contract ChatPane renders.

import { describe, expect, it } from "vitest";
import { greetingForHour, SUGGESTIONS } from "./greeting";

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
