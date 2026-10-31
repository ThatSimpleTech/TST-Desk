// Fireflies maths (TD-5202): seeded lights that wander near their homes
// and blink — lit for part of their own period, an ember otherwise, never
// all in step.

import { describe, expect, it } from "vitest";
import { FIREFLIES, fireflies, fireflyInto, glowAt } from "./fireflies";

const W = 1280;
const H = 800;
const flies = fireflies(0xf1ef1, W, H);
const spot = new Float32Array(2);

describe("fireflies", () => {
	it("is deterministic for a seed and box", () => {
		expect(fireflies(2, W, H)).toEqual(fireflies(2, W, H));
		expect(fireflies(2, W, H)).not.toEqual(fireflies(3, W, H));
	});

	it("scales its count and glow with the box", () => {
		expect(fireflies(1, 120, 75).length).toBe(FIREFLIES.minFlies);
		expect(fireflies(1, 20_000, 20_000).length).toBe(FIREFLIES.maxFlies);
		const tile = fireflies(1, 120, 75);
		expect(Math.max(...tile.map((f) => f.radius))).toBeLessThan(Math.min(...flies.map((f) => f.radius)) + 1);
	});

	it("keeps nearer the ground than the sky", () => {
		const low = flies.filter((f) => f.hy > H / 2).length;
		expect(low).toBeGreaterThan(flies.length / 2);
	});
});

describe("fireflyInto", () => {
	it("wanders, but never far from home", () => {
		for (const fly of flies) {
			const reach = fly.ax[0] + fly.ax[1] + fly.ay[0] + fly.ay[1];
			for (const t of [0, 7, 120, 9999]) {
				fireflyInto(spot, fly, t);
				expect(Math.hypot(spot[0] - fly.hx, spot[1] - fly.hy)).toBeLessThanOrEqual(reach + 1e-6);
			}
		}
	});

	it("moves smoothly", () => {
		const fly = flies[0];
		fireflyInto(spot, fly, 50);
		const [x, y] = [spot[0], spot[1]];
		fireflyInto(spot, fly, 50 + 1 / 30);
		expect(Math.hypot(spot[0] - x, spot[1] - y)).toBeLessThan(2);
	});
});

describe("glowAt", () => {
	it("stays between the ember and full light", () => {
		for (const fly of flies) {
			for (let t = 0; t < 30; t += 0.05) {
				const g = glowAt(fly, t);
				expect(g).toBeGreaterThanOrEqual(FIREFLIES.ember - 1e-9);
				expect(g).toBeLessThanOrEqual(1 + 1e-9);
			}
		}
	});

	it("blinks: each firefly reaches nearly full light and falls back to the ember", () => {
		for (const fly of flies) {
			let lo = 1;
			let hi = 0;
			for (let t = 0; t < fly.period; t += fly.period / 400) {
				const g = glowAt(fly, t);
				lo = Math.min(lo, g);
				hi = Math.max(hi, g);
			}
			expect(hi).toBeGreaterThan(0.95);
			expect(lo).toBeCloseTo(FIREFLIES.ember, 6);
		}
	});

	it("never blinks all in step", () => {
		const lit = flies.map((fly) => glowAt(fly, 3) > 0.5).filter(Boolean).length;
		expect(lit).toBeGreaterThan(0);
		expect(lit).toBeLessThan(flies.length);
	});

	it("handles time before zero", () => {
		const g = glowAt(flies[0], -3.5);
		expect(g).toBeGreaterThanOrEqual(FIREFLIES.ember - 1e-9);
		expect(g).toBeLessThanOrEqual(1);
	});
});
