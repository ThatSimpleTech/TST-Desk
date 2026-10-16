// Playful background core (TD-1750).
//
// A field of fine dust that twinkles slowly and drifts upward, the same
// idea as Claude Code's starfield: sparse specks on the ground colour,
// a little brighter where the ambient band lights the top of the pane.
// Pure and rune-free so the seeded randomness, the drift wrap, and the
// reduced-motion rule are unit-testable in node; the Svelte component
// owns the canvas, the animation loop, and reading the design tokens.
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

export interface Dust {
	x: number;
	y: number;
	/** Radius in CSS pixels at full twinkle. */
	r: number;
	/** Phase offset so specks never blink in lockstep. */
	phase: number;
	/** Twinkle speed in cycles per second. */
	cps: number;
	/** Per-speck brightness weight 0–1; most are faint, a few are bright. */
	bright: number;
}

export interface FieldOptions {
	seed: number;
	width: number;
	height: number;
	minR?: number;
	maxR?: number;
	minCps?: number;
	maxCps?: number;
}

export const DUST = {
	/** Specks per 100k CSS px²; density scales with area. */
	density: 9,
	minR: 0.5,
	maxR: 1.6,
	minCps: 0.03,
	maxCps: 0.12,
	/** Upward drift in CSS px per second. Barely perceptible; it is what
	 *  makes the field read as alive rather than painted on. */
	driftPxPerSec: 1.4,
} as const;

/** The seeded dust field. Deterministic for a given seed and size.
 *  Brightness is skewed faint — squaring the roll keeps most specks quiet
 *  and lets only a few shine, which is what reads as depth. */
export function dustField(opts: FieldOptions): Dust[] {
	const {
		minR = DUST.minR,
		maxR = DUST.maxR,
		minCps = DUST.minCps,
		maxCps = DUST.maxCps,
	} = opts;
	if (opts.width <= 0 || opts.height <= 0) return [];
	const rand = mulberry32(opts.seed);
	const count = Math.round((opts.width * opts.height) / 100_000) * DUST.density;
	const specks: Dust[] = [];
	for (let i = 0; i < count; i++) {
		specks.push({
			x: rand() * opts.width,
			y: rand() * opts.height,
			r: minR + rand() * (maxR - minR),
			phase: rand(),
			cps: minCps + rand() * (maxCps - minCps),
			bright: rand() * rand(),
		});
	}
	return specks;
}

/** Alpha 0–0.9 for one speck at time `tMs`. A slow sine with a soft floor,
 *  scaled by the speck's brightness weight (a quiet speck peaks at 0.5, a
 *  bright one at 0.9), so the field shimmers and never goes fully dark but
 *  stays quiet overall. */
export function twinkleAlpha(speck: Dust, tMs: number): number {
	const wave = 0.5 + 0.5 * Math.sin(2 * Math.PI * (speck.phase + (tMs / 1000) * speck.cps));
	return (0.1 + 0.8 * wave) * (0.5 + 0.5 * speck.bright);
}

/** A speck's y at time `tSec` after a slow upward drift, wrapped so specks
 *  that leave the top re-enter at the bottom. */
export function driftY(y: number, tSec: number, height: number): number {
	if (height <= 0) return y;
	const shifted = y - tSec * DUST.driftPxPerSec;
	return ((shifted % height) + height) % height;
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

/** Reduced motion kills the animation loop: the field holds a fixed mid
 *  twinkle, no drift, and ripples are skipped entirely. Static is the
 *  rule, not the exception (same stance as the GlowLayer). */
export function shouldAnimate(prefersReducedMotion: boolean): boolean {
	return !prefersReducedMotion;
}

/** Fixed alpha used for every speck in static mode (wave mid-point at
 * t = 0 under the sine above, minus the motion). */
export function staticAlpha(speck: Dust): number {
	return twinkleAlpha({ ...speck, phase: 0, cps: 0 }, 0);
}
