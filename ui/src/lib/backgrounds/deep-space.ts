// Deep space background maths (TD-5202): stars drift toward the viewer out
// of a point up and to the right of the middle, growing and brightening as
// they come, and now and then a shooting star crosses the sky.
//
// Every star's place is a pure function of its id and the time. Depth
// runs from 1 (far) down to `near`; a star that arrives starts again at the
// far end, somewhere new, picked by hashing its id with how many trips it
// has made. So a frame keeps no state, allocates nothing, and the same
// moment always looks the same. Shooting stars work the same way: time is
// cut into slots, and a hash of the slot number says whether one flies in
// it, where, and which way.

import { hash01, mulberry32, smoothstep } from "./random";

export const SPACE = {
	/** Depth where a star has passed the viewer and goes back to the far end. */
	near: 0.06,
	/** Depth crossed per second: a full trip takes about 47 s. */
	speed: 0.02,
	/** How far past the edges a star may land, as a share of the box. */
	overscan: 0.12,
	/** The depth at which a trip's stars spread evenly over the window. Each
	 *  trip is picked by where the star lands at this depth, so it is in view
	 *  for nearly its whole approach — true perspective with even starts
	 *  would leave a near star on screen for only a moment, and the field
	 *  would read flat. Farther than this, stars bunch toward the vanishing
	 *  point, as real ones do. */
	landing: 0.38,
	/** Where the stars stream from, as fractions of the box. */
	vanishX: 0.64,
	vanishY: 0.38,
	/** Stars per 100k CSS px², clamped. */
	per100k: 64,
	minStars: 44,
	maxStars: 1100,
	/** One chance of a shooting star per slot. */
	meteorSlotSec: 13,
	meteorChance: 0.7,
	meteorSec: 1.15,
} as const;

/** Colour buckets: most stars white, some cool, a few warm. */
export const SPACE_TONES = 3;

export interface SpaceField {
	seed: number;
	count: number;
	/** Each star's place in the seeded draw, before the tone sort: what its
	 *  trips are hashed by, so a star keeps its path when the count changes. */
	id: Uint32Array;
	/** How far into its trip each star is at t = 0, in depth units. */
	offset: Float32Array;
	/** Size multiplier 0.6–1.4. */
	size: Float32Array;
	tone: Uint8Array;
	/** Index where each tone's run starts; toneStart[SPACE_TONES] === count. */
	toneStart: Int32Array;
}

export function starTotal(width: number, height: number): number {
	if (width <= 0 || height <= 0) return 0;
	const n = ((width * height) / 100_000) * SPACE.per100k;
	return Math.round(Math.min(SPACE.maxStars, Math.max(SPACE.minStars, n)));
}

/** The seeded field for a box, stars sorted by tone. Every star takes the
 *  same three draws, so a bigger box keeps the smaller box's stars and adds
 *  more after them. */
export function spaceField(seed: number, width: number, height: number): SpaceField {
	const count = starTotal(width, height);
	const rand = mulberry32(seed);
	const span = 1 - SPACE.near;
	const drafts: { id: number; offset: number; size: number; tone: number }[] = [];
	for (let i = 0; i < count; i++) {
		const roll = rand();
		drafts.push({ id: i, offset: rand() * span, size: 0.6 + rand() * 0.8, tone: roll < 0.7 ? 0 : roll < 0.9 ? 1 : 2 });
	}
	drafts.sort((a, b) => a.tone - b.tone);
	const field: SpaceField = {
		seed,
		count,
		id: new Uint32Array(count),
		offset: new Float32Array(count),
		size: new Float32Array(count),
		tone: new Uint8Array(count),
		toneStart: new Int32Array(SPACE_TONES + 1),
	};
	drafts.forEach((d, i) => {
		field.id[i] = d.id;
		field.offset[i] = d.offset;
		field.size[i] = d.size;
		field.tone[i] = d.tone;
	});
	for (let tone = 0, i = 0; tone <= SPACE_TONES; tone++) {
		while (i < count && field.tone[i] < tone) i++;
		field.toneStart[tone] = i;
	}
	return field;
}

export interface SpaceLayout {
	vx: number;
	vy: number;
	/** CSS px a star at depth 1 sits from the vanishing point per focal unit. */
	focal: number;
	/** Star size multiplier: a small box (a preview tile) gets smaller
	 *  stars, so it looks like the window in miniature, not a close-up. */
	sizeScale: number;
	/** Where a star may land at the landing depth, in focal units: the box
	 *  plus the overscan. */
	left: number;
	right: number;
	top: number;
	bottom: number;
}

export function spaceLayout(width: number, height: number): SpaceLayout {
	const vx = width * SPACE.vanishX;
	const vy = height * SPACE.vanishY;
	const focal = Math.max(width, height, 1) * 0.5;
	const padX = width * SPACE.overscan;
	const padY = height * SPACE.overscan;
	return {
		vx,
		vy,
		focal,
		sizeScale: Math.min(1, Math.max(0.45, Math.min(width, height) / 560)),
		left: (-padX - vx) / focal,
		right: (width + padX - vx) / focal,
		top: (-padY - vy) / focal,
		bottom: (height + padY - vy) / focal,
	};
}

/** Star `i` at `tSec`: writes [x, y, z] — x and y in focal units at depth 1
 *  for this trip, z the depth (1 far → near). Trips hash the star's id, not
 *  its sorted place, which one added star would shift for every star after
 *  it. */
export function starInto(out: Float32Array, field: SpaceField, layout: SpaceLayout, i: number, tSec: number): void {
	const span = 1 - SPACE.near;
	const travel = Math.max(0, tSec) * SPACE.speed + field.offset[i];
	const trip = Math.floor(travel / span);
	const star = field.seed + field.id[i];
	out[0] = (layout.left + hash01(star, trip, 1) * (layout.right - layout.left)) * SPACE.landing;
	out[1] = (layout.top + hash01(star, trip, 2) * (layout.bottom - layout.top)) * SPACE.landing;
	out[2] = 1 - (travel - trip * span);
}

/** Where a star at (x, y, z) lands and how it looks: writes [sx, sy, radius,
 *  alpha]. A far star is a dim point that fades in rather than popping; a
 *  near one is bigger and brighter. */
export function projectInto(out: Float32Array, layout: SpaceLayout, x: number, y: number, z: number, size: number): void {
	const depth = Math.max(z, SPACE.near);
	const nearness = (1 - depth) / (1 - SPACE.near);
	out[0] = layout.vx + (x / depth) * layout.focal;
	out[1] = layout.vy + (y / depth) * layout.focal;
	out[2] = (0.45 + 2 * nearness ** 1.6) * size * layout.sizeScale;
	out[3] = smoothstep(1, 0.88, depth) * (0.35 + 0.65 * nearness);
}

/** The shooting star in flight at `tSec`, if any: writes [x0, y0, dx, dy,
 *  progress, length] with the start and length as fractions of the box and
 *  (dx, dy) a unit direction. Returns false when the sky is clear. */
export function meteorInto(out: Float32Array, seed: number, tSec: number): boolean {
	if (tSec < 0) return false;
	const slot = Math.floor(tSec / SPACE.meteorSlotSec);
	if (hash01(seed, slot, 7) > SPACE.meteorChance) return false;
	const start = hash01(seed, slot, 8) * (SPACE.meteorSlotSec - SPACE.meteorSec);
	const local = tSec - slot * SPACE.meteorSlotSec - start;
	if (local < 0 || local > SPACE.meteorSec) return false;
	const leftward = hash01(seed, slot, 11) < 0.5;
	const angle = ((18 + hash01(seed, slot, 12) * 22) * Math.PI) / 180;
	out[0] = 0.15 + hash01(seed, slot, 9) * 0.7;
	out[1] = 0.04 + hash01(seed, slot, 10) * 0.36;
	out[2] = Math.cos(angle) * (leftward ? -1 : 1);
	out[3] = Math.sin(angle);
	out[4] = local / SPACE.meteorSec;
	out[5] = 0.1 + hash01(seed, slot, 13) * 0.08;
	return true;
}
