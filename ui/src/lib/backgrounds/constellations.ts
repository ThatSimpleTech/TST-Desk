// Constellations background maths (TD-5202): stars wander slowly in
// straight lines and join up with faint lines whenever two come near, the
// line fading in as they close and out as they part — figures form,
// hold for a while, and dissolve.
//
// A star's place is its start plus its drift times the time, wrapped
// around a margin past the edges, so a star leaving one side returns on the
// other unseen and its lines never snap across the window. Link finding
// writes into arrays the caller allocated once.

import { countForArea, mulberry32, wrap } from "./random";

export const CONSTELLATIONS = {
	per100k: 9,
	minStars: 16,
	maxStars: 150,
	/** Drift speed range, CSS px a second. */
	minSpeed: 2.5,
	maxSpeed: 7,
	/** Link reach, in average spacings between stars. */
	reach: 1.08,
} as const;

export interface Drifter {
	x0: number;
	y0: number;
	vx: number;
	vy: number;
	/** Square side in CSS px. */
	size: number;
	/** Twinkle phase and speed (cycles a second), as the Classic dust. */
	phase: number;
	cps: number;
	/** 0–1; the brightest few get a halo. */
	bright: number;
}

export function drifters(seed: number, width: number, height: number): Drifter[] {
	const count = countForArea(width, height, CONSTELLATIONS.per100k, CONSTELLATIONS.minStars, CONSTELLATIONS.maxStars);
	const rand = mulberry32(seed);
	const stars: Drifter[] = [];
	for (let i = 0; i < count; i++) {
		const heading = rand() * Math.PI * 2;
		const speed = CONSTELLATIONS.minSpeed + rand() * (CONSTELLATIONS.maxSpeed - CONSTELLATIONS.minSpeed);
		const bright = rand() ** 2.2;
		stars.push({
			x0: rand() * width,
			y0: rand() * height,
			vx: Math.cos(heading) * speed,
			vy: Math.sin(heading) * speed,
			size: 1.1 + bright * 1.6,
			phase: rand(),
			cps: 0.04 + rand() * 0.08,
			bright,
		});
	}
	return stars;
}

/** How near two stars must come to be joined, CSS px: a little over the
 *  average spacing, so each star has two or three neighbours in reach. */
export function linkReach(width: number, height: number, count: number): number {
	if (count <= 0 || width <= 0 || height <= 0) return 0;
	return Math.sqrt((width * height) / count) * CONSTELLATIONS.reach;
}

/** Every star's place at `tSec`, wrapped `margin` px past each edge. */
export function positionsInto(
	xs: Float32Array,
	ys: Float32Array,
	stars: readonly Drifter[],
	tSec: number,
	width: number,
	height: number,
	margin: number,
): void {
	for (let i = 0; i < stars.length; i++) {
		const s = stars[i];
		xs[i] = wrap(s.x0 + s.vx * tSec, -margin, width + margin);
		ys[i] = wrap(s.y0 + s.vy * tSec, -margin, height + margin);
	}
}

/** Line opacity for two stars `distance` apart: full when they touch, fading
 *  smoothly to nothing at `reach`, so lines never pop. */
export function linkAlpha(distance: number, reach: number): number {
	if (reach <= 0 || distance >= reach) return 0;
	const t = 1 - Math.max(0, distance) / reach;
	return t * t;
}

/** Find the pairs within `reach`. Writes star indices into `pairs` (two per
 *  link) and each link's opacity into `alphas`; returns the link count,
 *  never more than the arrays hold. */
export function collectLinks(
	xs: Float32Array,
	ys: Float32Array,
	count: number,
	reach: number,
	pairs: Uint16Array,
	alphas: Float32Array,
): number {
	const room = Math.min(alphas.length, Math.floor(pairs.length / 2));
	const reach2 = reach * reach;
	let n = 0;
	for (let i = 0; i < count && n < room; i++) {
		for (let j = i + 1; j < count && n < room; j++) {
			const dx = xs[i] - xs[j];
			const dy = ys[i] - ys[j];
			const d2 = dx * dx + dy * dy;
			if (d2 >= reach2) continue;
			const alpha = linkAlpha(Math.sqrt(d2), reach);
			if (alpha <= 0.01) continue;
			pairs[n * 2] = i;
			pairs[n * 2 + 1] = j;
			alphas[n] = alpha;
			n++;
		}
	}
	return n;
}
