// Aurora maths (TD-5202): seeded curtains whose lower edges stay in the top
// half of the window and move smoothly, heights that stay positive, and a
// brightness that stays 0–1 with soft ends.

import { describe, expect, it } from "vitest";
import { AURORA, auroraRibbons, brightness, curtainHeight, edgeY } from "./aurora";

const ribbons = auroraRibbons(0xa0e0);

describe("auroraRibbons", () => {
	it("is deterministic for a seed, and changes with it", () => {
		expect(auroraRibbons(5)).toEqual(auroraRibbons(5));
		expect(auroraRibbons(5)).not.toEqual(auroraRibbons(6));
	});

	it("hangs the configured curtains, each spanning most of the width", () => {
		expect(ribbons).toHaveLength(AURORA.ribbons);
		for (const r of ribbons) {
			expect(r.from).toBeGreaterThanOrEqual(0);
			expect(r.to).toBeLessThanOrEqual(1);
			expect(r.to - r.from).toBeGreaterThanOrEqual(0.7 - 1e-9);
		}
	});
});

describe("curtain shape", () => {
	it("keeps every lower edge in the top of the window, clear of the composer", () => {
		for (const r of ribbons) {
			for (let x = 0; x <= 1; x += 0.01) {
				for (const t of [0, 13, 777, 54_321]) {
					const y = edgeY(r, x, t);
					expect(y).toBeGreaterThan(0);
					expect(y).toBeLessThanOrEqual(AURORA.floor);
					expect(curtainHeight(r, x, t)).toBeGreaterThan(0);
				}
			}
		}
	});

	it("moves smoothly: a thirtieth of a second barely shifts the edge", () => {
		for (const r of ribbons) {
			for (let x = 0; x <= 1; x += 0.05) {
				expect(Math.abs(edgeY(r, x, 100 + 1 / 30) - edgeY(r, x, 100))).toBeLessThan(0.002);
			}
		}
	});

	it("does move over a minute", () => {
		const r = ribbons[0];
		let moved = 0;
		for (let x = 0; x <= 1; x += 0.05) moved += Math.abs(edgeY(r, x, 60) - edgeY(r, x, 0));
		expect(moved).toBeGreaterThan(0.05);
	});
});

describe("brightness", () => {
	it("stays 0–1 everywhere, for any ray depth", () => {
		for (const r of ribbons) {
			for (let x = 0; x <= 1; x += 0.01) {
				for (const depth of [0, 0.12, 0.45, 1, 3]) {
					const b = brightness(r, x, 42, depth);
					expect(b).toBeGreaterThanOrEqual(0);
					expect(b).toBeLessThanOrEqual(1);
				}
			}
		}
	});

	it("is dark beyond a curtain's ends", () => {
		const r = { ...ribbons[0], from: 0.3, to: 0.7 };
		expect(brightness(r, 0.1, 5)).toBe(0);
		expect(brightness(r, 0.9, 5)).toBe(0);
		expect(brightness(r, 0.5, 5)).toBeGreaterThan(0);
	});

	it("is smoother with shallow rays: less spread from strip to strip", () => {
		const r = { ...ribbons[1], from: 0, to: 1 };
		const spread = (depth: number) => {
			let total = 0;
			for (let x = 0.2; x < 0.8; x += 0.004) total += Math.abs(brightness(r, x + 0.004, 9, depth) - brightness(r, x, 9, depth));
			return total;
		};
		expect(spread(0.1)).toBeLessThan(spread(0.6));
	});
});
