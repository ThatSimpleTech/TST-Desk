// Classic's motion (TD-5202): the orbit halo's turn and breathing scale, its
// wash, the pulses of its ring dots and its anchor, and the drift of the two
// soft bands under the chat header, all as functions of one clock.
//
// They were six CSS animations at the display's rate, each on frames of its
// own. Where WebKitGTK paints in software (no GL), every one of those frames
// repainted the whole window, the app drawn over it included, and kept the
// page process near a whole core. Now one shared loop steps them together,
// capped at CLASSIC_FPS ticks a second (about 10 in WebKitGTK), and the dust
// paints on the same tick, so the window repaints once a tick. The curves are
// the old keyframes and timing functions, so the look at any moment is what
// the CSS drew at that moment.

import { createSharedLoop, windowFrameHost, type SharedLoop } from "./loop";

/** Most ticks a second: the cap, not what runs. A 60 Hz display meets it
 *  exactly, but WebKitGTK's frame timer runs a little fast (about 62 Hz),
 *  so five frames land just short of a twelfth of a second and the loop
 *  ticks on every sixth, about 10 times a second. That is fine: the
 *  outermost ring turns once in 80 s about 610 px from the core of a
 *  1200×800 window, so its dots travel about 48 px a second and step 4–5 px
 *  a tick, under the 7 px of a soft rim dot, so its trail stays continuous
 *  (the inner rings step a pixel or less). Each tick repaints the window,
 *  so the cost grows with the rate (DECISIONS.md has the numbers). */
export const CLASSIC_FPS = 12;

/** A CSS cubic-bezier() timing function: progress in, eased progress out. */
export function cubicBezier(x1: number, y1: number, x2: number, y2: number): (x: number) => number {
	const cx = 3 * x1;
	const bx = 3 * (x2 - x1) - cx;
	const ax = 1 - cx - bx;
	const cy = 3 * y1;
	const by = 3 * (y2 - y1) - cy;
	const ay = 1 - cy - by;
	const curveX = (t: number): number => ((ax * t + bx) * t + cx) * t;
	const curveY = (t: number): number => ((ay * t + by) * t + cy) * t;
	const slopeX = (t: number): number => (3 * ax * t + 2 * bx) * t + cx;
	return (x) => {
		if (x <= 0) return 0;
		if (x >= 1) return 1;
		// Newton's method converges in a few steps on these curves; bisection
		// catches the flat spots where it would not.
		let t = x;
		for (let i = 0; i < 8; i++) {
			const miss = curveX(t) - x;
			if (Math.abs(miss) < 1e-7) return curveY(t);
			const slope = slopeX(t);
			if (Math.abs(slope) < 1e-6) break;
			t -= miss / slope;
		}
		let lo = 0;
		let hi = 1;
		t = x;
		while (hi - lo > 1e-7) {
			if (curveX(t) < x) lo = t;
			else hi = t;
			t = (lo + hi) / 2;
		}
		return curveY(t);
	};
}

/** CSS `ease-in-out`, the orbit's breathing. */
const EASE_IN_OUT = cubicBezier(0.42, 0, 0.58, 1);

/** The `--ease-out` token in tokens.css, the bands' drift (classic.test.ts
 *  holds the two together). */
export const EASE_OUT_CURVE = [0.2, 0.7, 0.2, 1] as const;
const EASE_OUT = cubicBezier(...EASE_OUT_CURVE);

const TURN_MS = 80_000;
const WASH_MS = 14_000;
const GLOW_MS = 9_000;
const ANCHOR_MS = 6_000;
const BAND_MS = 18_000;
const WARM_BAND_MS = 26_000;

function fraction(x: number): number {
	return x - Math.floor(x);
}

/** The old pulse keyframes: `low` at 0% and 100%, 1 at 50%, eased in and
 *  out on each half of `periodMs`. */
function pulse(clockMs: number, periodMs: number, low: number): number {
	const p = fraction(clockMs / periodMs);
	return p < 0.5
		? low + (1 - low) * EASE_IN_OUT(p * 2)
		: 1 - (1 - low) * EASE_IN_OUT(p * 2 - 1);
}

/** An `alternate` animation's eased progress: forward on even passes,
 *  backward on odd ones; `reverse` starts backward (`alternate-reverse`). */
function swing(clockMs: number, periodMs: number, reverse: boolean): number {
	const passes = clockMs / periodMs;
	const backward = (Math.floor(passes) % 2 === 1) !== reverse;
	const u = fraction(passes);
	return EASE_OUT(backward ? 1 - u : u);
}

export interface OrbitPose {
	/** The ring field's turn, degrees. */
	turnDeg: number;
	/** The field's breathing scale. */
	scale: number;
	/** Opacity of the wash behind the core. */
	wash: number;
	/** Opacity of the ring dots. */
	glow: number;
	/** Opacity of the anchor dot. */
	anchor: number;
}

export function orbitPose(clockMs: number): OrbitPose {
	const turn = fraction(clockMs / TURN_MS);
	return {
		turnDeg: turn * 360,
		// Linear keyframes: 1, then 1.02 half way round, then 1 again.
		scale: 1 + 0.02 * (1 - Math.abs(turn * 2 - 1)),
		wash: pulse(clockMs, WASH_MS, 0.75),
		glow: pulse(clockMs, GLOW_MS, 0.7),
		anchor: pulse(clockMs, ANCHOR_MS, 0.7),
	};
}

/** How far each header band has drifted: 0 at home, 1 at a tenth of its
 *  width to the right and 12 px down. */
export interface BandDrift {
	accent: number;
	warm: number;
}

export function bandDrift(clockMs: number): BandDrift {
	return {
		accent: swing(clockMs, BAND_MS, false),
		warm: swing(clockMs, WARM_BAND_MS, true),
	};
}

/** The one loop every part of Classic steps on: the orbit behind the window,
 *  and the header bands and the dust in the chat pane. Its host rests while
 *  the window is hidden or out of focus. */
export const classicTick: SharedLoop = createSharedLoop(() => windowFrameHost(window), { fps: CLASSIC_FPS });
