// Playful background core (TD-1750).
//
// The sparkle field the chat pane floats on, and a ring of light that
// expands from wherever the user clicks. Pure and rune-free so the layout,
// the seeded randomness, and the reduced-motion rule are unit-testable in
// node; the Svelte component owns the canvas, the animation loop, and
// reading the design tokens.
//
// Colours arrive as resolved hex strings from the caller — the module
// never hardcodes a colour, so the tokens in tokens.css stay the only
// palette in the tree (AGENTS §6).

/** Deterministic PRNG (mulberry32). Same seed, same field, every load. */
export function mulberry32(seed: number): () => number {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

export interface Sparkle {
	x: number;
	y: number;
	/** Radius in CSS pixels at full twinkle. */
	r: number;
	/** Phase offset so sparkles never blink in lockstep. */
	phase: number;
	/** Twinkle speed in cycles per second. */
	cps: number;
}

export interface FieldOptions {
	seed: number;
	width: number;
	height: number;
	/** Sparkles per 100k CSS px²; density scales with area. */
	density?: number;
	minR?: number;
	maxR?: number;
	minCps?: number;
	maxCps?: number;
}

export const DEFAULT_FIELD = {
	density: 4,
	minR: 0.7,
	maxR: 2.2,
	minCps: 0.05,
	maxCps: 0.2,
} as const;

/** The static sparkle field. Deterministic for a given seed and size. */
export function sparkleField(opts: FieldOptions): Sparkle[] {
	const {
		density = DEFAULT_FIELD.density,
		minR = DEFAULT_FIELD.minR,
		maxR = DEFAULT_FIELD.maxR,
		minCps = DEFAULT_FIELD.minCps,
		maxCps = DEFAULT_FIELD.maxCps,
	} = opts;
	if (opts.width <= 0 || opts.height <= 0) return [];
	const rand = mulberry32(opts.seed);
	const count = Math.round((opts.width * opts.height) / 100_000) * density;
	const sparkles: Sparkle[] = [];
	for (let i = 0; i < count; i++) {
		sparkles.push({
			x: rand() * opts.width,
			y: rand() * opts.height,
			r: minR + rand() * (maxR - minR),
			phase: rand(),
			cps: minCps + rand() * (maxCps - minCps),
		});
	}
	return sparkles;
}

/** Alpha 0–1 for one sparkle at time `tMs`. A slow sine with a soft floor
 *  so the field shimmers and never goes fully dark. */
export function twinkleAlpha(sparkle: Sparkle, tMs: number): number {
	const wave = 0.5 + 0.5 * Math.sin(2 * Math.PI * (sparkle.phase + (tMs / 1000) * sparkle.cps));
	return 0.2 + 0.6 * wave;
}

// ── Click ripples ───────────────────────────────────────────────────────────
//
// A click anywhere in the pane births one ring at the pointer: it expands
// ease-out and fades on a sine envelope (in quickly, out slowly), so the
// light arrives and leaves without popping. Ripples die on their own age;
// a cap keeps a mash of clicks from stacking unbounded.

export interface Ripple {
	x: number;
	y: number;
	/** Seconds since the click. */
	ageSec: number;
	/** Per-ripple random scale (0.7–1.1 of the max radius), seeded by the caller. */
	scale: number;
}

export const RIPPLE = {
	/** Seconds from click to gone. */
	durationSec: 0.9,
	/** Radius in CSS px a ripple reaches at full expansion (× its scale). */
	maxRadius: 260,
	/** Peak alpha at mid-life. */
	peakAlpha: 0.45,
	/** Most ripples alive at once; the oldest is replaced beyond this. */
	maxRipples: 6,
} as const;

/** Birth one ripple at a click point. `seed` makes each ripple's size
 *  deterministic for a given click, so tests can pin it. */
export function rippleAt(x: number, y: number, seed: number): Ripple {
	const rand = mulberry32(seed);
	return { x, y, ageSec: 0, scale: 0.7 + rand() * 0.4 };
}

/** Ease-out cubic on [0,1] — fast out of the gate, settling at the edge. */
export function easeOutCubic(p: number): number {
	const c = Math.min(Math.max(p, 0), 1);
	return 1 - (1 - c) ** 3;
}

/** Ring radius in CSS px at `tSec` after the click. */
export function rippleRadius(ripple: Ripple, tSec: number): number {
	const p = Math.min(Math.max(tSec / RIPPLE.durationSec, 0), 1);
	return RIPPLE.maxRadius * ripple.scale * easeOutCubic(p);
}

/** Ring alpha at `tSec`: sine envelope — zero at birth and death, peak
 *  mid-life — so the light fades in and out instead of popping. */
export function rippleAlpha(ripple: Ripple, tSec: number): number {
	const p = Math.min(Math.max(tSec / RIPPLE.durationSec, 0), 1);
	return Math.sin(Math.PI * p) * RIPPLE.peakAlpha;
}

/** Age every ripple by `dtSec` and drop the ones past their duration.
 *  Pure: returns a new array. */
export function stepRipples(ripples: Ripple[], dtSec: number): Ripple[] {
	return ripples
		.map((r) => ({ ...r, ageSec: r.ageSec + dtSec }))
		.filter((r) => r.ageSec <= RIPPLE.durationSec);
}

/** Add a ripple to the list, enforcing the live cap by dropping the oldest. */
export function pushRipple(ripples: Ripple[], ripple: Ripple): Ripple[] {
	const next = [...ripples, ripple];
	return next.length > RIPPLE.maxRipples ? next.slice(next.length - RIPPLE.maxRipples) : next;
}

/** Reduced motion kills the animation loop: sparkles hold a fixed mid
 *  twinkle and ripples are skipped entirely. Static is the rule, not the
 *  exception (same stance as the GlowLayer). */
export function shouldAnimate(prefersReducedMotion: boolean): boolean {
	return !prefersReducedMotion;
}

/** Fixed alpha used for every sparkle in static mode (wave mid-point at
 *  t = 0 under the sine above, minus the motion). */
export function staticAlpha(): number {
	return twinkleAlpha({ x: 0, y: 0, r: 1, phase: 0, cps: 0 }, 0);
}
