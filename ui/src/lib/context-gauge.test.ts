// Context gauge helpers and `/compact` parsing (TD-5109).

import { describe, it, expect } from "vitest";
import {
	COMPACT_GUIDANCE_MAX,
	compactNotSentCopy,
	formatTokens,
	gaugeLabel,
	gaugeLevel,
	gaugePercent,
	parseCompactCommand,
	type ContextReading,
} from "./context-gauge";

const reading = (used: number, window = 65536, compactAt = 49152): ContextReading => ({
	tier: "brain",
	used_tokens: used,
	context_window: window,
	compact_at: compactAt,
});

describe("parseCompactCommand", () => {
	it("takes /compact alone", () => {
		expect(parseCompactCommand("/compact")).toEqual({ guidance: "" });
		expect(parseCompactCommand("  /compact  ")).toEqual({ guidance: "" });
		expect(parseCompactCommand("/COMPACT")).toEqual({ guidance: "" });
	});

	it("takes what follows as guidance, across lines", () => {
		expect(parseCompactCommand("/compact keep the parser work")).toEqual({
			guidance: "keep the parser work",
		});
		expect(parseCompactCommand("/compact\nkeep the\ntests")).toEqual({
			guidance: "keep the\ntests",
		});
	});

	it("leaves other text for the model", () => {
		expect(parseCompactCommand("/compactor")).toBeNull();
		expect(parseCompactCommand("please /compact")).toBeNull();
		expect(parseCompactCommand("/review")).toBeNull();
		expect(parseCompactCommand("")).toBeNull();
	});

	it("caps guidance at what the daemon keeps", () => {
		const parsed = parseCompactCommand(`/compact ${"x".repeat(900)}`);
		expect(parsed!.guidance.length).toBe(COMPACT_GUIDANCE_MAX);
	});
});

describe("formatTokens", () => {
	it.each([
		[0, "0"],
		[980, "980"],
		[1234, "1.2k"],
		[12_345, "12k"],
		[32_768, "32k"],
		[65_536, "64k"],
		[131_072, "128k"],
		[200_000, "200k"],
		[1_000_000, "1M"],
		[1_500_000, "1.5M"],
	])("%d reads %s", (n, label) => {
		expect(formatTokens(n)).toBe(label);
	});
});

describe("gauge", () => {
	it("labels used against the window", () => {
		expect(gaugeLabel(reading(12_345))).toBe("12k / 64k");
	});

	it("reports the share of the whole window", () => {
		expect(gaugePercent(reading(32_768))).toBe(50);
		expect(gaugePercent(reading(90_000))).toBe(100);
		expect(gaugePercent(reading(10, 0))).toBe(0);
	});

	it("warns in the last quarter before auto-compaction", () => {
		expect(gaugeLevel(reading(10_000))).toBe("ok");
		expect(gaugeLevel(reading(37_000))).toBe("near");
		expect(gaugeLevel(reading(49_152))).toBe("over");
		expect(gaugeLevel(reading(5, 100, 0))).toBe("ok");
	});
});

describe("compactNotSentCopy (TD-5154)", () => {
	it("blames a running turn only when there is one", () => {
		expect(compactNotSentCopy(true)).toEqual({
			severity: "toast",
			title: "Not compacted",
			body: "A turn is running. Compact when it finishes, or stop it first.",
		});
		const idle = compactNotSentCopy(false);
		expect(idle.severity).toBe("toast");
		expect(idle.body).toContain("isn't connected");
		expect(idle.body).not.toContain("turn");
	});
});
