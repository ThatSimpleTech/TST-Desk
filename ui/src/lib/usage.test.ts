// Tests for the usage view's pure derivations (TD-1706).
//
// Dollar figures here are hand-computed from the token counts and the
// per-1M prices in the comments, the same discipline the core rollup
// tests hold (AGENTS §7) — the folding must not lose or invent money.

import { describe, it, expect } from "vitest";
import type { UsageRollup } from "./protocol";
import {
	BUCKET_LABELS,
	USAGE_BUCKETS,
	formatBucketKey,
	formatTokens,
	groupByBucket,
	rowTokens,
	totalsFor,
} from "./usage";

function row(over: Partial<UsageRollup> = {}): UsageRollup {
	return {
		bucket: "day",
		key: "2026-08-17",
		tier: "brain",
		prompt_tokens: 0,
		cached_prompt_tokens: 0,
		completion_tokens: 0,
		cost: 0,
		classifier_cost: 0,
		...over,
	};
}

describe("rowTokens", () => {
	it("counts prompt, cached, and completion together", () => {
		expect(
			rowTokens(
				row({ prompt_tokens: 40_000, cached_prompt_tokens: 10_000, completion_tokens: 5_000 }),
			),
		).toBe(55_000);
	});

	it("a row that moved nothing is zero, not NaN", () => {
		expect(rowTokens(row())).toBe(0);
	});
});

describe("groupByBucket", () => {
	const rows: UsageRollup[] = [
		// Thursday: brain $0.195, worker $0.008 -> $0.203
		row({ bucket: "day", key: "2026-08-13", tier: "brain", prompt_tokens: 40_000, cost: 0.195 }),
		row({ bucket: "day", key: "2026-08-13", tier: "worker", prompt_tokens: 10_000, cost: 0.008 }),
		// Wednesday: brain $0.03
		row({ bucket: "day", key: "2026-08-12", tier: "brain", prompt_tokens: 10_000, cost: 0.03 }),
		// A different bucket kind riding the same message.
		row({ bucket: "session", key: "sess-a", tier: "brain", prompt_tokens: 50_000, cost: 0.225 }),
		row({ bucket: "week", key: "2026-08-10", tier: "brain", prompt_tokens: 50_000, cost: 0.225 }),
	];

	it("keeps only the asked-for bucket kind", () => {
		expect(groupByBucket(rows, "session").map((g) => g.key)).toEqual(["sess-a"]);
		expect(groupByBucket(rows, "week").map((g) => g.key)).toEqual(["2026-08-10"]);
	});

	it("folds tiers into one group per key and sums the money", () => {
		const groups = groupByBucket(rows, "day");
		expect(groups).toHaveLength(2);
		expect(groups[0].key).toBe("2026-08-13");
		expect(groups[0].rows.map((r) => r.tier)).toEqual(["brain", "worker"]);
		expect(groups[0].cost).toBeCloseTo(0.203, 10);
		expect(groups[0].tokens).toBe(50_000);
		expect(groups[1].cost).toBeCloseTo(0.03, 10);
	});

	it("preserves the daemon's ordering rather than sorting by key", () => {
		// Session ids do not sort chronologically; re-sorting here would
		// claim alphabetical order was recency.
		const unsorted = [
			row({ bucket: "session", key: "zzz-newest", cost: 0.1 }),
			row({ bucket: "session", key: "aaa-older", cost: 0.2 }),
		];
		expect(groupByBucket(unsorted, "session").map((g) => g.key)).toEqual([
			"zzz-newest",
			"aaa-older",
		]);
	});

	it("keeps classifier spend on its own total", () => {
		const groups = groupByBucket(
			[row({ bucket: "day", key: "d", cost: 0.016, classifier_cost: 0.0008 })],
			"day",
		);
		expect(groups[0].cost).toBeCloseTo(0.016, 10);
		expect(groups[0].classifierCost).toBeCloseTo(0.0008, 10);
	});

	it("no rows means no groups", () => {
		expect(groupByBucket([], "day")).toEqual([]);
	});
});

describe("totalsFor", () => {
	it("adds every group's money and tokens", () => {
		const groups = groupByBucket(
			[
				row({ key: "a", prompt_tokens: 10_000, cost: 0.03 }),
				row({ key: "b", prompt_tokens: 40_000, completion_tokens: 5_000, cost: 0.195 }),
				row({ key: "b", tier: "worker", prompt_tokens: 10_000, cost: 0.008 }),
			],
			"day",
		);
		const totals = totalsFor(groups);
		expect(totals.cost).toBeCloseTo(0.233, 10);
		expect(totals.tokens).toBe(65_000);
	});

	it("an empty view totals to zero, not NaN", () => {
		expect(totalsFor([])).toEqual({ tokens: 0, cost: 0, classifierCost: 0 });
	});
});

describe("formatTokens", () => {
	it("shows small counts exactly", () => {
		expect(formatTokens(0)).toBe("0");
		expect(formatTokens(999)).toBe("999");
	});

	it("abbreviates thousands and millions", () => {
		expect(formatTokens(1000)).toBe("1.0k");
		expect(formatTokens(55_000)).toBe("55.0k");
		expect(formatTokens(999_999)).toBe("1000.0k");
		expect(formatTokens(1_000_000)).toBe("1.00M");
		expect(formatTokens(2_450_000)).toBe("2.45M");
	});
});

describe("formatBucketKey", () => {
	it("labels a week by the Monday it opened on", () => {
		expect(formatBucketKey("week", "2026-08-10")).toBe("week of 2026-08-10");
	});

	it("leaves a day as the ISO date", () => {
		expect(formatBucketKey("day", "2026-08-17")).toBe("2026-08-17");
	});

	it("shortens a long session id to its distinguishing tail", () => {
		expect(formatBucketKey("session", "0f8c2a11-4b7e-4d90-a1c3-9e77b0d2f451")).toBe("…b0d2f451");
	});

	it("leaves a short session id whole", () => {
		expect(formatBucketKey("session", "sess-1")).toBe("sess-1");
	});
});

describe("bucket metadata", () => {
	it("every bucket has a label", () => {
		for (const bucket of USAGE_BUCKETS) {
			expect(BUCKET_LABELS[bucket]).toBeTruthy();
		}
		expect(USAGE_BUCKETS).toEqual(["session", "day", "week"]);
	});
});
