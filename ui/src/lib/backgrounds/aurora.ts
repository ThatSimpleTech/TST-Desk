// Aurora background maths (TD-5202): three curtains of light hang across the
// top of the window. Each lower edge ripples slowly, each curtain's height
// breathes along its length, and fine vertical rays drift along it — the
// look of a real aurora's folds. Everything is a sum of slow sines of
// position and time, seeded per curtain, so a frame keeps no state and the
// lower half of the window, where the composer sits, stays clear.

import { mulberry32, smoothstep } from "./random";

export const AURORA = {
	ribbons: 3,
	/** Strip width in CSS px. The curtains are drawn soft (a fraction of the
	 *  window's resolution), so wider strips cost fewer draws and still blur
	 *  into one sheet; the finest rays are still five strips wide. */
	columnPx: 9,
	/** Fewest strips a curtain gets, so a small box (a preview tile, drawn at
	 *  full resolution) still shows a curtain, not a bar chart. */
	minColumns: 48,
	/** The folds drift a few pixels a second: 20 frames is smooth. */
	fps: 20,
	/** Lowest a curtain's lower edge may sit, 0–1 of the height. */
	floor: 0.56,
} as const;

/** amp (0–1 of height, or of brightness), freq (cycles across the width),
 *  speed (radians a second), phase (radians). */
export interface Wave {
	amp: number;
	freq: number;
	speed: number;
	phase: number;
}

export interface Ribbon {
	/** Rest height of the lower edge, 0–1 of the box height. */
	base: number;
	/** Rest curtain height, 0–1 of the box height. */
	height: number;
	/** Where the curtain starts and ends across the width, 0–1. */
	from: number;
	to: number;
	edge: Wave[];
	lift: Wave[];
	glow: Wave[];
	rays: Wave[];
	/** Peak opacity. */
	alpha: number;
	/** Colour pair index: 0 low→high, 1 mid→high, 2 low→mid. */
	tone: number;
}

function wave(rand: () => number, amp: number, freqLo: number, freqHi: number, speedLo: number, speedHi: number): Wave {
	const direction = rand() < 0.5 ? -1 : 1;
	return {
		amp: amp * (0.7 + rand() * 0.6),
		freq: freqLo + rand() * (freqHi - freqLo),
		speed: direction * (speedLo + rand() * (speedHi - speedLo)),
		phase: rand() * Math.PI * 2,
	};
}

export function auroraRibbons(seed: number): Ribbon[] {
	const rand = mulberry32(seed);
	const ribbons: Ribbon[] = [];
	for (let i = 0; i < AURORA.ribbons; i++) {
		const from = rand() * 0.25;
		ribbons.push({
			base: 0.2 + i * 0.07 + rand() * 0.05,
			height: 0.13 + rand() * 0.09,
			from,
			to: Math.min(1, from + 0.7 + rand() * 0.3),
			edge: [wave(rand, 0.075, 0.3, 0.7, 0.04, 0.1), wave(rand, 0.025, 1.1, 2.2, 0.08, 0.18)],
			lift: [wave(rand, 0.25, 0.5, 1.3, 0.05, 0.13), wave(rand, 0.1, 1.8, 3.2, 0.1, 0.22)],
			glow: [wave(rand, 0.32, 0.5, 1.4, 0.07, 0.18), wave(rand, 0.14, 1.8, 3.6, 0.12, 0.26)],
			rays: [wave(rand, 0.6, 7, 12, 0.18, 0.36), wave(rand, 0.4, 17, 26, 0.3, 0.6)],
			alpha: 0.75 + rand() * 0.25,
			tone: i % 3,
		});
	}
	return ribbons;
}

function sum(waves: Wave[], x01: number, tSec: number): number {
	let total = 0;
	for (const w of waves) total += w.amp * Math.sin(2 * Math.PI * w.freq * x01 + w.speed * tSec + w.phase);
	return total;
}

/** The curtain's lower edge at `x01` across the box, 0–1 of its height. */
export function edgeY(ribbon: Ribbon, x01: number, tSec: number): number {
	return Math.min(AURORA.floor, ribbon.base + sum(ribbon.edge, x01, tSec));
}

/** How far the curtain reaches up from its lower edge, 0–1 of the height. */
export function curtainHeight(ribbon: Ribbon, x01: number, tSec: number): number {
	return ribbon.height * Math.max(0.25, 1 + sum(ribbon.lift, x01, tSec));
}

/** Opacity 0–1 at `x01`: slow swells of light along the curtain, rays
 *  drifting through them, and soft ends where the curtain starts and stops.
 *  `rayDepth` (0–1) is how much the rays cut into the light: crisp folds on
 *  a night sky, near-smooth silk on paper. */
export function brightness(ribbon: Ribbon, x01: number, tSec: number, rayDepth = 0.4): number {
	const taper = 0.15;
	const ends = smoothstep(ribbon.from, ribbon.from + taper, x01) * (1 - smoothstep(ribbon.to - taper, ribbon.to, x01));
	if (ends <= 0) return 0;
	const swell = Math.min(1, Math.max(0.06, 0.55 + sum(ribbon.glow, x01, tSec)));
	const depth = Math.min(1, Math.max(0, rayDepth));
	const rays = 1 - depth + depth * (0.5 + 0.5 * sum(ribbon.rays, x01, tSec));
	return Math.min(1, Math.max(0, ribbon.alpha * ends * swell * rays));
}
