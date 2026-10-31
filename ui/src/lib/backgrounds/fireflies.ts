// Fireflies background maths (TD-5202): a few warm lights wander on slow,
// looping paths and blink — each lights up for a second or two, goes dark,
// and lights again somewhere a little further on, never in step with the
// others.
//
// A path is two slow sines a side around a home point, so it loops without
// ever retracing itself exactly. The blink is a soft pulse over part of
// each firefly's own period with a faint ember left between pulses, so the
// window is never quite empty. Pure functions of time: no state per frame.

import { countForArea, mulberry32 } from "./random";

export const FIREFLIES = {
	per100k: 4.2,
	minFlies: 8,
	maxFlies: 60,
	/** Shortest and longest blink period, seconds. */
	minPeriod: 4,
	maxPeriod: 9,
	/** Glow left between pulses. */
	ember: 0.1,
} as const;

export interface Firefly {
	hx: number;
	hy: number;
	/** Two sines a side: amplitude (CSS px), angular speed (rad/s), phase. */
	ax: [number, number];
	wx: [number, number];
	px: [number, number];
	ay: [number, number];
	wy: [number, number];
	py: [number, number];
	period: number;
	/** Where in its period it starts, 0–1. */
	offset: number;
	/** Share of the period it is lit, 0–1. */
	duty: number;
	/** Glow radius in CSS px at full light. */
	radius: number;
}

export function fireflies(seed: number, width: number, height: number): Firefly[] {
	const count = countForArea(width, height, FIREFLIES.per100k, FIREFLIES.minFlies, FIREFLIES.maxFlies);
	const rand = mulberry32(seed);
	// Wander and glow scale with the box, so a preview tile looks like the
	// window in miniature.
	const roam = Math.min(width, height) * 0.09;
	const glow = Math.min(1, Math.max(0.4, Math.min(width, height) / 520));
	const flies: Firefly[] = [];
	for (let i = 0; i < count; i++) {
		flies.push({
			hx: rand() * width,
			// Fewer up high: fireflies keep near the ground.
			hy: height * (0.2 + 0.8 * Math.sqrt(rand())),
			ax: [roam * (0.6 + rand()), roam * (0.2 + rand() * 0.5)],
			wx: [0.05 + rand() * 0.08, 0.13 + rand() * 0.17],
			px: [rand() * Math.PI * 2, rand() * Math.PI * 2],
			ay: [roam * (0.4 + rand() * 0.8), roam * (0.15 + rand() * 0.4)],
			wy: [0.04 + rand() * 0.08, 0.11 + rand() * 0.18],
			py: [rand() * Math.PI * 2, rand() * Math.PI * 2],
			period: FIREFLIES.minPeriod + rand() * (FIREFLIES.maxPeriod - FIREFLIES.minPeriod),
			offset: rand(),
			duty: 0.4 + rand() * 0.25,
			radius: (15 + rand() * 14) * glow,
		});
	}
	return flies;
}

/** Where a firefly is at `tSec`: writes [x, y]. */
export function fireflyInto(out: Float32Array, fly: Firefly, tSec: number): void {
	out[0] = fly.hx + fly.ax[0] * Math.sin(fly.wx[0] * tSec + fly.px[0]) + fly.ax[1] * Math.sin(fly.wx[1] * tSec + fly.px[1]);
	out[1] = fly.hy + fly.ay[0] * Math.sin(fly.wy[0] * tSec + fly.py[0]) + fly.ay[1] * Math.sin(fly.wy[1] * tSec + fly.py[1]);
}

/** How lit a firefly is at `tSec`, 0–1: a soft pulse over the lit share of
 *  its period, the ember otherwise. */
export function glowAt(fly: Firefly, tSec: number): number {
	const cycle = (((tSec / fly.period + fly.offset) % 1) + 1) % 1;
	if (cycle >= fly.duty) return FIREFLIES.ember;
	const pulse = Math.sin((Math.PI * cycle) / fly.duty);
	return FIREFLIES.ember + (1 - FIREFLIES.ember) * pulse * pulse;
}
