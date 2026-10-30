// Galaxy background maths (TD-5202): a two-armed spiral seen at a tilt,
// turning about once every ten minutes.
//
// Stars live in the galaxy's own plane, as a radius (0–1 of the disc) and
// an angle. The disc turns as one body: a real galaxy's inner stars lap the
// outer ones, but that would wind the arms up within the hour, and a
// background has to look the same at five o'clock as it did at nine. The
// arms are logarithmic spirals that trail the turn, a bright bulge sits at
// the core, a faint even disc fills between the arms, and dust lanes run
// along the leading edge of each arm. Everything is seeded; nothing here
// touches a canvas.

import { gaussian, mulberry32, smoothstep } from "./random";

export const GALAXY = {
	arms: 2,
	/** Pitch of the arms in degrees; wider opens the spiral up. */
	pitchDeg: 20,
	/** Disc radius (0–1) where the arms leave the bulge. */
	armStart: 0.07,
	/** How flat the tilted disc looks: on-screen height over width. */
	tilt: 0.44,
	/** On-screen angle of the disc's long axis, in degrees. */
	positionAngleDeg: -21,
	/** Seconds per full turn. */
	turnSec: 600,
	/** Shares of the stars in the bulge and the arms; the rest is even disc. */
	bulgeShare: 0.22,
	armShare: 0.62,
	/** Star count bounds; the count follows the disc's on-screen area. */
	minStars: 500,
	maxStars: 2600,
} as const;

/** Colour buckets. Stars are sorted by tone so a frame sets each colour once. */
export const TONE = { core: 0, arm: 1, disc: 2 } as const;
export const TONES = 3;

export interface GalaxyStars {
	count: number;
	/** Radius, 0–1 of the disc. */
	r: Float32Array;
	/** Angle in the disc plane at spin 0, radians. */
	angle: Float32Array;
	/** Square side in CSS px. */
	size: Float32Array;
	/** Base opacity 0–1. */
	alpha: Float32Array;
	tone: Uint8Array;
	/** Index where each tone's run starts; toneStart[TONES] === count. */
	toneStart: Int32Array;
}

const PITCH = (GALAXY.pitchDeg * Math.PI) / 180;

/** Angle of arm `arm` at radius `r`: a log spiral whose angle falls as the
 *  radius grows, so the arms trail a disc turning toward larger angles. */
export function armAngle(r: number, arm: number): number {
	return (arm * 2 * Math.PI) / GALAXY.arms - Math.log(Math.max(r, 1e-6) / GALAXY.armStart) / Math.tan(PITCH);
}

/** Stars to draw for a disc of `radius` CSS px on screen. */
export function starCount(radius: number): number {
	const area = Math.PI * radius * radius * GALAXY.tilt;
	return Math.round(Math.min(GALAXY.maxStars, Math.max(GALAXY.minStars, (area / 100_000) * 300)));
}

interface Draft {
	r: number;
	angle: number;
	size: number;
	alpha: number;
	tone: number;
}

function bulgeStar(rand: () => number): Draft {
	const r = Math.min(Math.abs(gaussian(rand)) * 0.085, 0.32);
	return {
		r,
		angle: rand() * Math.PI * 2,
		size: 0.6 + rand() * 1.0,
		alpha: 0.4 + rand() * 0.55,
		tone: TONE.core,
	};
}

function armStar(rand: () => number): Draft {
	const arm = Math.floor(rand() * GALAXY.arms);
	const base = GALAXY.armStart + (1 - GALAXY.armStart) * rand() ** 1.2;
	const r = Math.min(1, Math.max(0.02, base + gaussian(rand) * 0.018));
	// Arms fan out toward the rim; the inner arm is a tight thread.
	const spread = 0.07 + 0.2 * r;
	// Older yellow stars crowd the inner arms; young blue ones the outer.
	const old = r < 0.35 && rand() < 0.35;
	const young = rand() < 0.045;
	return {
		r,
		angle: armAngle(r, arm) + gaussian(rand) * spread,
		size: young ? 1.5 + rand() * 0.9 : 0.5 + rand() * 0.9,
		alpha: (0.3 + rand() * 0.6) * (1 - 0.65 * smoothstep(0.7, 1, r)),
		tone: old ? TONE.core : TONE.arm,
	};
}

function discStar(rand: () => number): Draft {
	return {
		r: Math.sqrt(rand()),
		angle: rand() * Math.PI * 2,
		size: 0.4 + rand() * 0.7,
		alpha: 0.12 + rand() * 0.3,
		tone: TONE.disc,
	};
}

/** The seeded star list, sorted by tone. Deterministic for a seed and count. */
export function galaxyStars(seed: number, count: number): GalaxyStars {
	const rand = mulberry32(seed);
	const n = Math.max(0, Math.floor(count));
	const drafts: Draft[] = [];
	const bulge = Math.round(n * GALAXY.bulgeShare);
	const arms = Math.round(n * GALAXY.armShare);
	for (let i = 0; i < n; i++) {
		drafts.push(i < bulge ? bulgeStar(rand) : i < bulge + arms ? armStar(rand) : discStar(rand));
	}
	// Stable sort keeps the seeded order inside each tone.
	drafts.sort((a, b) => a.tone - b.tone);
	const stars: GalaxyStars = {
		count: n,
		r: new Float32Array(n),
		angle: new Float32Array(n),
		size: new Float32Array(n),
		alpha: new Float32Array(n),
		tone: new Uint8Array(n),
		toneStart: new Int32Array(TONES + 1),
	};
	drafts.forEach((d, i) => {
		stars.r[i] = d.r;
		stars.angle[i] = d.angle;
		stars.size[i] = d.size;
		stars.alpha[i] = d.alpha;
		stars.tone[i] = d.tone;
	});
	for (let tone = 0, i = 0; tone <= TONES; tone++) {
		while (i < n && stars.tone[i] < tone) i++;
		stars.toneStart[tone] = i;
	}
	return stars;
}

export interface GalaxyLayout {
	cx: number;
	cy: number;
	/** Disc radius in CSS px. */
	radius: number;
	cosPa: number;
	sinPa: number;
	tilt: number;
}

/** Where the galaxy sits in a box: the core up and to the right of the
 *  middle, clear of the chat column, the disc wide enough that the arms
 *  sweep the whole window. */
export function galaxyLayout(width: number, height: number): GalaxyLayout {
	const pa = (GALAXY.positionAngleDeg * Math.PI) / 180;
	return {
		cx: width * 0.7,
		cy: height * 0.36,
		radius: Math.max(width * 0.5, height * 0.74),
		cosPa: Math.cos(pa),
		sinPa: Math.sin(pa),
		tilt: GALAXY.tilt,
	};
}

/** The disc's turn at `clockMs`, radians. */
export function spinAt(clockMs: number): number {
	return ((clockMs / 1000) * 2 * Math.PI) / GALAXY.turnSec;
}

/** Screen position of a disc point, written into `out` so a frame of a few
 *  thousand stars allocates nothing. */
export function projectInto(out: Float32Array, layout: GalaxyLayout, r: number, angle: number, spin: number): void {
	const a = angle + spin;
	const px = r * layout.radius * Math.cos(a);
	const py = r * layout.radius * Math.sin(a) * layout.tilt;
	out[0] = layout.cx + px * layout.cosPa - py * layout.sinPa;
	out[1] = layout.cy + px * layout.sinPa + py * layout.cosPa;
}

/** A soft stamp on the face-on glow sprite, in disc units (−1…1). */
export interface GlowStamp {
	x: number;
	y: number;
	/** Radius in disc units. */
	radius: number;
	alpha: number;
}

/** Where an arm's light sits off its centre line: a fixed distance in disc
 *  units, so the offset reads the same near the core as at the rim. */
function offArm(r: number, distance: number): number {
	return distance / Math.max(r, 0.05);
}

/** The luminous haze along each arm, brightest inside, fading at the rim.
 *  Many faint stamps overlap into one smooth band. */
export function armGlow(seed: number, perArm: number): GlowStamp[] {
	const rand = mulberry32(seed);
	const out: GlowStamp[] = [];
	for (let arm = 0; arm < GALAXY.arms; arm++) {
		for (let i = 0; i < perArm; i++) {
			const r = GALAXY.armStart + (1 - GALAXY.armStart) * rand() ** 1.1;
			const angle = armAngle(r, arm) + gaussian(rand) * (0.04 + 0.12 * r);
			out.push({
				x: r * Math.cos(angle),
				y: r * Math.sin(angle),
				radius: 0.04 + 0.08 * r,
				alpha: 0.075 * (1 - 0.75 * r),
			});
		}
	}
	return out;
}

/** Dust lanes: dark threads just ahead of each arm (the side the disc turns
 *  toward), carved out of the glow. Stamps march evenly along the lane and
 *  overlap, so the lane is one soft thread, not a row of beads. */
export function dustLanes(seed: number, perArm: number): GlowStamp[] {
	const rand = mulberry32(seed);
	const out: GlowStamp[] = [];
	for (let arm = 0; arm < GALAXY.arms; arm++) {
		for (let i = 0; i < perArm; i++) {
			const r = 0.09 + 0.58 * (i / perArm) + (rand() - 0.5) * 0.004;
			const angle = armAngle(r, arm) + offArm(r, 0.042) + (rand() - 0.5) * 0.01;
			const fade = smoothstep(0.09, 0.17, r) * (1 - smoothstep(0.48, 0.67, r));
			out.push({ x: r * Math.cos(angle), y: r * Math.sin(angle), radius: 0.014 + 0.022 * r, alpha: 0.11 * fade });
		}
	}
	return out;
}

/** Knots of glowing gas strung along the arms' middle reaches. */
export function nebulaKnots(seed: number, count: number): GlowStamp[] {
	const rand = mulberry32(seed);
	const out: GlowStamp[] = [];
	for (let i = 0; i < count; i++) {
		const arm = i % GALAXY.arms;
		const r = 0.22 + rand() * 0.6;
		const angle = armAngle(r, arm) - offArm(r, 0.01) + gaussian(rand) * 0.05;
		out.push({
			x: r * Math.cos(angle),
			y: r * Math.sin(angle),
			radius: 0.008 + rand() * 0.016,
			alpha: 0.16 + rand() * 0.26,
		});
	}
	return out;
}
