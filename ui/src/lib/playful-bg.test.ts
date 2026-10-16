// Playful background core tests (TD-1750). The seeded randomness, the
// twinkle envelope, the drift wrap, and the reduced-motion rule are the
// contract the canvas component draws from.

import { describe, it, expect } from "vitest";
import {
	DUST,
	driftY,
	dustField,
	easeOutCubic,
	pushRipple,
	rippleAlpha,
	rippleAt,
	rippleRadius,
	RIPPLE,
	staticAlpha,
	shouldAnimate,
	stepRipples,
	twinkleAlpha,
	type Ripple,
} from "./playful-bg";

describe("dustField", () => {
	it("is deterministic for a given seed and size", () => {
		expect(dustField({ seed: 7, width: 1200, height: 800 })).toEqual(
			dustField({ seed: 7, width: 1200, height: 800 }),
		);
	});

	it("changes with the seed", () => {
		expect(dustField({ seed: 7, width: 1200, height: 800 })).not.toEqual(
			dustField({ seed: 8, width: 1200, height: 800 }),
		);
	});

	it("scales density with area", () => {
		const small = dustField({ seed: 1, width: 400, height: 300 });
		const big = dustField({ seed: 1, width: 1600, height: 1200 });
		expect(big.length).toBeGreaterThan(small.length);
	});

	it("keeps every speck inside the field, every radius in range, and every brightness weight in 0–1", () => {
		for (const s of dustField({ seed: 42, width: 900, height: 600 })) {
			expect(s.x).toBeGreaterThanOrEqual(0);
			expect(s.x).toBeLessThanOrEqual(900);
			expect(s.y).toBeGreaterThanOrEqual(0);
			expect(s.y).toBeLessThanOrEqual(600);
			expect(s.r).toBeGreaterThanOrEqual(DUST.minR);
			expect(s.r).toBeLessThanOrEqual(DUST.maxR);
			expect(s.bright).toBeGreaterThanOrEqual(0);
			expect(s.bright).toBeLessThanOrEqual(1);
		}
	});

	it("skews brightness faint — most specks quiet, only a few bright", () => {
		const field = dustField({ seed: 11, width: 1200, height: 800 });
		const brightOnes = field.filter((s) => s.bright > 0.7).length;
		expect(brightOnes).toBeLessThan(field.length * 0.35);
	});

	it("returns nothing for an empty field", () => {
		expect(dustField({ seed: 1, width: 0, height: 500 })).toEqual([]);
	});
});

describe("twinkleAlpha", () => {
	it("stays inside its 0–0.9 envelope over a full cycle", () => {
		for (const s of dustField({ seed: 3, width: 1200, height: 800 })) {
			for (let t = 0; t <= 20_000; t += 37) {
				const a = twinkleAlpha(s, t);
				expect(a).toBeGreaterThanOrEqual(0);
				expect(a).toBeLessThanOrEqual(0.9 + 1e-9);
			}
		}
	});

	it("never exceeds the envelope peak for a fully bright speck", () => {
		const a = twinkleAlpha({ x: 0, y: 0, r: 1, phase: 0.25, cps: 0.12, bright: 1 }, 1250);
		expect(a).toBeGreaterThan(0);
		expect(a).toBeLessThanOrEqual(0.9 + 1e-9);
	});

	it("is deterministic in time", () => {
		const s = dustField({ seed: 5, width: 800, height: 600 })[0];
		expect(twinkleAlpha(s, 4200)).toBe(twinkleAlpha(s, 4200));
	});
});

describe("driftY", () => {
	it("wraps into the field at all times", () => {
		for (const t of [0, 1, 37.5, 1e6]) {
			const y = driftY(300, t, 600);
			expect(y).toBeGreaterThanOrEqual(0);
			expect(y).toBeLessThan(600);
		}
	});

	it("moves upward at the configured rate and wraps", () => {
		expect(driftY(300, 0, 600)).toBe(300);
		expect(driftY(300, 1, 600)).toBeCloseTo(300 - DUST.driftPxPerSec, 6);
		// Past the top edge it re-enters at the bottom.
		expect(driftY(1, 100, 600)).toBeCloseTo(600 - ((100 * DUST.driftPxPerSec - 1) % 600), 6);
	});
});

describe("ripples", () => {
	it("births a deterministic ripple at the click point", () => {
		expect(rippleAt(100, 200, 7)).toEqual(rippleAt(100, 200, 7));
		expect(rippleAt(100, 200, 7)).toMatchObject({ x: 100, y: 200, ageSec: 0 });
	});

	it("ease-out goes 0 at 0 and 1 at 1, clamped outside", () => {
		expect(easeOutCubic(0)).toBe(0);
		expect(easeOutCubic(1)).toBe(1);
		expect(easeOutCubic(-1)).toBe(0);
		expect(easeOutCubic(2)).toBe(1);
	});

	it("expands over its life and fades in then out", () => {
		const r = rippleAt(50, 50, 3);
		expect(rippleAlpha(r, 0)).toBeCloseTo(0, 10);
		expect(rippleAlpha(r, RIPPLE.durationSec)).toBeCloseTo(0, 10);
		expect(rippleAlpha(r, RIPPLE.durationSec / 2)).toBeCloseTo(RIPPLE.peakAlpha, 6);
		expect(rippleRadius(r, RIPPLE.durationSec)).toBeCloseTo(RIPPLE.maxRadius * r.scale, 6);
	});

	it("age drops ripples past their duration and keeps young ones", () => {
		const young = rippleAt(10, 10, 1);
		const old = rippleAt(20, 20, 2);
		old.ageSec = RIPPLE.durationSec - 0.01;
		const after = stepRipples([young, old], 0.05);
		expect(after).toEqual([expect.objectContaining({ ageSec: 0.05 })]);
	});

	it("caps the live ripples, dropping the oldest", () => {
		let list: Ripple[] = [];
		for (let i = 0; i < RIPPLE.maxRipples + 3; i++) list = pushRipple(list, rippleAt(i, i, i));
		expect(list).toHaveLength(RIPPLE.maxRipples);
		// The three oldest clicks are gone; the newest survive in order.
		expect(list[0].x).toBe(3);
		expect(list[list.length - 1].x).toBe(RIPPLE.maxRipples + 2);
	});
});

describe("motion rules", () => {
	it("animates normally and stops under reduced motion", () => {
		expect(shouldAnimate(false)).toBe(true);
		expect(shouldAnimate(true)).toBe(false);
	});

	it("static alpha is the mid-envelope value used when frozen", () => {
		// (0.12 + 0.88·0.5) × (0.25 + 0.75·bright); for bright=1 → 0.5.
		expect(staticAlpha({ x: 0, y: 0, r: 1, phase: 0, cps: 0, bright: 1 })).toBeCloseTo(0.5, 10);
	});
});
