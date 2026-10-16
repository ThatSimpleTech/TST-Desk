// Playful background core tests (TD-1750). The seeded randomness, the
// twinkle envelope, the click-ripple easing, and the reduced-motion rule
// are the contract the canvas component draws from.

import { describe, it, expect } from "vitest";
import {
	easeOutCubic,
	pushRipple,
	rippleAlpha,
	rippleAt,
	rippleRadius,
	sparkleField,
	staticAlpha,
	stepRipples,
	shouldAnimate,
	twinkleAlpha,
	RIPPLE,
} from "./playful-bg";

describe("sparkleField", () => {
	it("is deterministic for a given seed and size", () => {
		expect(sparkleField({ seed: 7, width: 1200, height: 800 })).toEqual(
			sparkleField({ seed: 7, width: 1200, height: 800 }),
		);
	});

	it("changes with the seed", () => {
		expect(sparkleField({ seed: 7, width: 1200, height: 800 })).not.toEqual(
			sparkleField({ seed: 8, width: 1200, height: 800 }),
		);
	});

	it("scales density with area", () => {
		const small = sparkleField({ seed: 1, width: 400, height: 300 });
		const big = sparkleField({ seed: 1, width: 1600, height: 1200 });
		expect(big.length).toBeGreaterThan(small.length);
	});

	it("keeps every sparkle inside the field and every radius in range", () => {
		for (const s of sparkleField({ seed: 42, width: 900, height: 600 })) {
			expect(s.x).toBeGreaterThanOrEqual(0);
			expect(s.x).toBeLessThanOrEqual(900);
			expect(s.y).toBeGreaterThanOrEqual(0);
			expect(s.y).toBeLessThanOrEqual(600);
			expect(s.r).toBeGreaterThanOrEqual(0.7);
			expect(s.r).toBeLessThanOrEqual(2.2);
		}
	});

	it("returns nothing for an empty field", () => {
		expect(sparkleField({ seed: 1, width: 0, height: 500 })).toEqual([]);
	});
});

describe("twinkleAlpha", () => {
	it("stays inside its 0.20–0.80 envelope over a full cycle", () => {
		for (const s of sparkleField({ seed: 3, width: 1200, height: 800 })) {
			for (let t = 0; t <= 10_000; t += 37) {
				const a = twinkleAlpha(s, t);
				expect(a).toBeGreaterThanOrEqual(0.2 - 1e-9);
				expect(a).toBeLessThanOrEqual(0.8 + 1e-9);
			}
		}
	});

	it("never drops to zero, so the field keeps a floor", () => {
		expect(twinkleAlpha({ x: 0, y: 0, r: 1, phase: 0.25, cps: 0.2 }, 1250)).toBeGreaterThan(0);
	});

	it("is deterministic in time", () => {
		const s = sparkleField({ seed: 5, width: 800, height: 600 })[0];
		expect(twinkleAlpha(s, 4200)).toBe(twinkleAlpha(s, 4200));
	});
});

describe("easeOutCubic", () => {
	it("runs 0→1, fast first, clamped at both ends", () => {
		expect(easeOutCubic(0)).toBe(0);
		expect(easeOutCubic(1)).toBe(1);
		expect(easeOutCubic(-0.5)).toBe(0);
		expect(easeOutCubic(1.5)).toBe(1);
		// Fast out of the gate: at a quarter time it is well past a quarter
		// of the way out; at three quarters it is nearly done.
		expect(easeOutCubic(0.25)).toBeGreaterThan(0.5);
		expect(easeOutCubic(0.75)).toBeGreaterThan(0.9);
	});
});

describe("ripples", () => {
	it("birth at the click point with a deterministic scale", () => {
		const a = rippleAt(120, 340, 99);
		const b = rippleAt(120, 340, 99);
		expect(a).toEqual({ x: 120, y: 340, ageSec: 0, scale: b.scale });
		expect(a.scale).toBeGreaterThanOrEqual(0.7);
		expect(a.scale).toBeLessThanOrEqual(1.1);
	});

	it("expand ease-out and fade in/out on the sine envelope", () => {
		const r = rippleAt(100, 100, 7);
		expect(rippleAlpha(r, 0)).toBe(0); // invisible at birth…
		expect(rippleRadius(r, 0)).toBe(0);
		const mid = rippleAlpha(r, RIPPLE.durationSec / 2);
		expect(mid).toBeCloseTo(RIPPLE.peakAlpha, 10); // …peak mid-life…
		expect(rippleRadius(r, RIPPLE.durationSec / 2)).toBeGreaterThan(rippleRadius(r, RIPPLE.durationSec / 4));
		expect(rippleAlpha(r, RIPPLE.durationSec)).toBeCloseTo(0, 9); // …gone at death
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
		let list: typeof rippleList = [];
		const rippleList: ReturnType<typeof rippleAt>[] = [];
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
		// 0.2 + 0.6 * 0.5 = 0.5 (wave mid-point at t=0, phase 0).
		expect(staticAlpha()).toBeCloseTo(0.5, 10);
	});
});
