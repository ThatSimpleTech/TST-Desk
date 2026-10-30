// Seeded randomness for the backgrounds (TD-5202). Nothing here uses the
// unseeded browser random source: the same seed gives the same sky on every
// load, and the maths is testable to the digit (paint.test.ts holds every
// module in this folder to that).

export { mulberry32 } from "../playful-bg";

/** A standard normal draw (Box–Muller) from a uniform source. */
export function gaussian(rand: () => number): number {
	const u = Math.max(rand(), 1e-12);
	const v = rand();
	return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/** A uniform 0–1 from up to three integers, the same every time. Per-frame
 *  lookups (where a star comes back after it passes the viewer) call this
 *  instead of keeping state, so a frame allocates nothing. */
export function hash01(a: number, b: number, c = 0): number {
	// The salt keeps (0, 0, 0) from hashing to 0.
	let h = 0x811c9dc5 ^ Math.imul(a | 0, 0x27d4eb2d) ^ Math.imul(b | 0, 0x165667b1) ^ Math.imul(c | 0, 0x9e3779b1);
	h = Math.imul(h ^ (h >>> 15), 0x85ebca6b);
	h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
	h ^= h >>> 16;
	return (h >>> 0) / 4294967296;
}

/** Smooth 0→1 step between `edge0` and `edge1`, clamped outside. */
export function smoothstep(edge0: number, edge1: number, x: number): number {
	if (edge0 === edge1) return x < edge0 ? 0 : 1;
	const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
	return t * t * (3 - 2 * t);
}

/** `n` scaled by window area (per 100k CSS px²), clamped to [min, max]. */
export function countForArea(width: number, height: number, per100k: number, min: number, max: number): number {
	if (width <= 0 || height <= 0) return 0;
	return Math.round(Math.min(max, Math.max(min, ((width * height) / 100_000) * per100k)));
}

/** Wrap `x` into [lo, hi). */
export function wrap(x: number, lo: number, hi: number): number {
	const span = hi - lo;
	if (span <= 0) return lo;
	return ((((x - lo) % span) + span) % span) + lo;
}
