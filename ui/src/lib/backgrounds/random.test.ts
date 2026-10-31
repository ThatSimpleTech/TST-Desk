// Seeded helpers for the backgrounds (TD-5202): a hash that is stable and
// spread, a smoothstep, a wrap, an area count and a skyfield that is the
// same every load.

import { describe, expect, it } from "vitest";
import { countForArea, gaussian, hash01, mulberry32, smoothstep, wrap } from "./random";
import { skyField } from "./sky";

describe("hash01", () => {
	it("is stable, in [0, 1), and not stuck at zero for zeros", () => {
		expect(hash01(1, 2, 3)).toBe(hash01(1, 2, 3));
		expect(hash01(0, 0, 0)).not.toBe(0);
		for (let i = 0; i < 1000; i++) {
			const h = hash01(i, i * 7, 3);
			expect(h).toBeGreaterThanOrEqual(0);
			expect(h).toBeLessThan(1);
		}
	});

	it("spreads evenly enough to place stars", () => {
		const buckets = new Array(10).fill(0);
		for (let i = 0; i < 10_000; i++) buckets[Math.floor(hash01(i, 17, 1) * 10)]++;
		for (const n of buckets) {
			expect(n).toBeGreaterThan(800);
			expect(n).toBeLessThan(1200);
		}
	});
});

describe("gaussian", () => {
	it("centres on zero with unit spread", () => {
		const rand = mulberry32(5);
		let sum = 0;
		let sq = 0;
		const n = 20_000;
		for (let i = 0; i < n; i++) {
			const g = gaussian(rand);
			sum += g;
			sq += g * g;
		}
		expect(Math.abs(sum / n)).toBeLessThan(0.05);
		expect(Math.sqrt(sq / n)).toBeCloseTo(1, 1);
	});
});

describe("smoothstep, wrap and countForArea", () => {
	it("smoothstep clamps and eases", () => {
		expect(smoothstep(0, 1, -1)).toBe(0);
		expect(smoothstep(0, 1, 2)).toBe(1);
		expect(smoothstep(0, 1, 0.5)).toBe(0.5);
		expect(smoothstep(1, 0, 0.25)).toBeCloseTo(1 - smoothstep(0, 1, 0.25), 10);
	});

	it("wrap folds any value into its range", () => {
		expect(wrap(5, 0, 10)).toBe(5);
		expect(wrap(12, 0, 10)).toBe(2);
		expect(wrap(-3, 0, 10)).toBe(7);
		expect(wrap(-130, -100, 1100)).toBe(1070);
	});

	it("countForArea scales with area inside its bounds", () => {
		expect(countForArea(0, 10, 5, 1, 9)).toBe(0);
		expect(countForArea(100, 100, 5, 3, 50)).toBe(3);
		expect(countForArea(1000, 1000, 5, 3, 500)).toBe(50);
		expect(countForArea(10_000, 10_000, 5, 3, 500)).toBe(500);
	});
});

describe("skyField", () => {
	it("is the same every load and fills only what it is asked to", () => {
		const opts = { seed: 3, width: 800, height: 600, per100k: 20, minR: 0.3, maxR: 1, top: 0.5 };
		expect(skyField(opts)).toEqual(skyField(opts));
		for (const s of skyField(opts)) {
			expect(s.y).toBeLessThanOrEqual(300);
			expect(s.r).toBeGreaterThanOrEqual(0.3);
			expect(s.r).toBeLessThanOrEqual(1);
		}
		expect(skyField({ ...opts, width: 0 })).toEqual([]);
	});
});
