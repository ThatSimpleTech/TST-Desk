// TD-5185: the read-only line under each tier in Settings.
import { describe, it, expect } from "vitest";
import { cardLine, cardNote } from "./model-card";
import type { TierCard } from "./protocol";

const base: TierCard = {
	input_price: 2,
	output_price: 10,
	cache_read_price: 0.2,
	context_window: 1048576,
	max_output_tokens: 65536,
};

describe("cardLine", () => {
	it("shows the stored numbers as sent", () => {
		expect(cardLine(base)).toBe(
			"$2.00 in / $10.00 out per million, cache $0.20, 1048576 context",
		);
	});

	it("keeps sub-cent prices readable", () => {
		expect(cardLine({ ...base, input_price: 0.075, cache_read_price: 0.0075 })).toContain(
			"$0.075 in",
		);
	});
});

describe("cardNote", () => {
	it("says nothing before any refresh", () => {
		expect(cardNote(base)).toBeNull();
	});

	it("names a miss and says the values are from config", () => {
		const note = cardNote({
			...base,
			card: "not_listed",
			detail: "https://openrouter.ai/api/v1 does not list x/y.",
		});
		expect(note).toContain("does not list x/y");
		expect(note).toContain("from config.yaml");
	});

	it("marks values that came from the card", () => {
		expect(cardNote({ ...base, card: "ok" })).toBe("From the model card.");
	});

	it("says a card with no price left only the prices alone", () => {
		const note = cardNote({ ...base, card: "no_pricing", detail: "x publishes no price for y." });
		expect(note).toBe("The server lists no price; prices are from config.yaml.");
	});
});
