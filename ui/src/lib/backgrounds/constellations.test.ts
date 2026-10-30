// Constellations maths (TD-5202): seeded drifting stars that wrap out of
// sight, lines that fade in and out with distance instead of popping, and
// link finding that respects the caller's fixed-size arrays.

import { describe, expect, it } from "vitest";
import { CONSTELLATIONS, collectLinks, drifters, linkAlpha, linkReach, positionsInto } from "./constellations";

describe("drifters", () => {
	it("is deterministic for a seed and box", () => {
		expect(drifters(3, 1280, 800)).toEqual(drifters(3, 1280, 800));
		expect(drifters(3, 1280, 800)).not.toEqual(drifters(4, 1280, 800));
	});

	it("scales with the box within bounds, and drifts slowly", () => {
		expect(drifters(1, 120, 75).length).toBe(CONSTELLATIONS.minStars);
		expect(drifters(1, 9000, 9000).length).toBe(CONSTELLATIONS.maxStars);
		for (const s of drifters(1, 1280, 800)) {
			const speed = Math.hypot(s.vx, s.vy);
			expect(speed).toBeGreaterThanOrEqual(CONSTELLATIONS.minSpeed - 1e-9);
			expect(speed).toBeLessThanOrEqual(CONSTELLATIONS.maxSpeed + 1e-9);
			expect(s.bright).toBeGreaterThanOrEqual(0);
			expect(s.bright).toBeLessThanOrEqual(1);
		}
	});
});

describe("positionsInto", () => {
	const stars = drifters(8, 1000, 600);
	const xs = new Float32Array(stars.length);
	const ys = new Float32Array(stars.length);

	it("starts every star at its seeded place", () => {
		positionsInto(xs, ys, stars, 0, 1000, 600, 100);
		expect(xs[0]).toBeCloseTo(stars[0].x0, 3);
		expect(ys[0]).toBeCloseTo(stars[0].y0, 3);
	});

	it("wraps every star inside the margin, however long it has drifted", () => {
		for (const t of [1, 600, 86_400]) {
			positionsInto(xs, ys, stars, t, 1000, 600, 100);
			for (let i = 0; i < stars.length; i++) {
				expect(xs[i]).toBeGreaterThanOrEqual(-100);
				expect(xs[i]).toBeLessThan(1100);
				expect(ys[i]).toBeGreaterThanOrEqual(-100);
				expect(ys[i]).toBeLessThan(700);
			}
		}
	});
});

describe("links", () => {
	it("reach a little over the average spacing", () => {
		const reach = linkReach(1000, 600, 60);
		expect(reach).toBeCloseTo(Math.sqrt(10_000) * CONSTELLATIONS.reach, 6);
		expect(linkReach(1000, 600, 0)).toBe(0);
	});

	it("fade smoothly from full to nothing at the reach", () => {
		expect(linkAlpha(0, 100)).toBe(1);
		expect(linkAlpha(100, 100)).toBe(0);
		expect(linkAlpha(150, 100)).toBe(0);
		expect(linkAlpha(50, 100)).toBeGreaterThan(linkAlpha(60, 100));
		expect(linkAlpha(99.9, 100)).toBeLessThan(0.001);
	});

	it("join exactly the pairs within reach, once each", () => {
		const xs = Float32Array.from([0, 50, 300, 320, 1000]);
		const ys = Float32Array.from([0, 0, 0, 10, 1000]);
		const pairs = new Uint16Array(40);
		const alphas = new Float32Array(20);
		const n = collectLinks(xs, ys, 5, 100, pairs, alphas);
		expect(n).toBe(2);
		expect(Array.from(pairs.slice(0, 4))).toEqual([0, 1, 2, 3]);
		expect(alphas[0]).toBeCloseTo(linkAlpha(50, 100), 6);
	});

	it("never write past the arrays they were given", () => {
		const count = 30;
		const xs = new Float32Array(count);
		const ys = new Float32Array(count);
		const pairs = new Uint16Array(10);
		const alphas = new Float32Array(5);
		expect(collectLinks(xs, ys, count, 100, pairs, alphas)).toBe(5);
	});
});
