// Galaxy maths (TD-5202): a seeded, tone-sorted star list whose arm stars
// sit on trailing log spirals, a projection that tilts and turns the disc,
// and glow stamps that stay inside it.

import { describe, expect, it } from "vitest";
import {
	GALAXY,
	TONE,
	TONES,
	armAngle,
	armGlow,
	dustLanes,
	galaxyLayout,
	galaxyStars,
	nebulaKnots,
	projectInto,
	spinAt,
	starCount,
} from "./galaxy";

/** Smallest angle between two directions, radians. */
function angleGap(a: number, b: number): number {
	const d = (((a - b) % (2 * Math.PI)) + 3 * Math.PI) % (2 * Math.PI);
	return Math.abs(d - Math.PI);
}

describe("galaxyStars", () => {
	it("is deterministic for a seed, and changes with it", () => {
		expect(galaxyStars(7, 900)).toEqual(galaxyStars(7, 900));
		expect(galaxyStars(7, 900).angle).not.toEqual(galaxyStars(8, 900).angle);
	});

	it("makes exactly the stars asked for, every one inside the disc", () => {
		const stars = galaxyStars(3, 1200);
		expect(stars.count).toBe(1200);
		for (let i = 0; i < stars.count; i++) {
			expect(stars.r[i]).toBeGreaterThanOrEqual(0);
			expect(stars.r[i]).toBeLessThanOrEqual(1);
			expect(stars.alpha[i]).toBeGreaterThan(0);
			expect(stars.alpha[i]).toBeLessThanOrEqual(1);
			expect(stars.size[i]).toBeGreaterThan(0);
		}
	});

	it("sorts by tone and marks where each tone starts, so a frame sets each colour once", () => {
		const stars = galaxyStars(11, 1500);
		for (let i = 1; i < stars.count; i++) expect(stars.tone[i]).toBeGreaterThanOrEqual(stars.tone[i - 1]);
		expect(stars.toneStart[0]).toBe(0);
		expect(stars.toneStart[TONES]).toBe(stars.count);
		for (let tone = 0; tone < TONES; tone++) {
			for (let i = stars.toneStart[tone]; i < stars.toneStart[tone + 1]; i++) expect(stars.tone[i]).toBe(tone);
		}
	});

	it("crowds the core: the bulge is warm and close in", () => {
		const stars = galaxyStars(5, 2000);
		const inner = stars.r.filter((r) => r < 0.3).length;
		const outer = stars.r.filter((r) => r > 0.7).length;
		expect(inner).toBeGreaterThan(outer);
		const cores = stars.toneStart[TONE.core + 1] - stars.toneStart[TONE.core];
		expect(cores).toBeGreaterThan(2000 * GALAXY.bulgeShare * 0.9);
	});

	it("puts arm stars on their spirals", () => {
		const stars = galaxyStars(9, 2000);
		let near = 0;
		let total = 0;
		for (let i = stars.toneStart[TONE.arm]; i < stars.toneStart[TONE.arm + 1]; i++) {
			const r = stars.r[i];
			const gap = Math.min(...Array.from({ length: GALAXY.arms }, (_, arm) => angleGap(stars.angle[i], armAngle(r, arm))));
			total++;
			if (gap < 0.5) near++;
		}
		expect(total).toBeGreaterThan(500);
		expect(near / total).toBeGreaterThan(0.8);
	});

	it("handles nothing", () => {
		expect(galaxyStars(1, 0).count).toBe(0);
		expect(galaxyStars(1, -5).count).toBe(0);
	});

	it("keeps every star when a resize changes the count, so the sky does not churn", () => {
		const key = (s: ReturnType<typeof galaxyStars>, i: number) =>
			[s.r[i], s.angle[i], s.size[i], s.alpha[i], s.tone[i]].join(",");
		const keys = (s: ReturnType<typeof galaxyStars>) => Array.from({ length: s.count }, (_, i) => key(s, i));
		const small = keys(galaxyStars(13, 1699));
		for (const more of [1700, 1701, 1760]) {
			const big = new Set(keys(galaxyStars(13, more)));
			expect(small.filter((k) => !big.has(k)), `${more} stars`).toEqual([]);
			expect(big.size).toBe(more);
		}
	});
});

describe("arms", () => {
	it("trail the turn: the arm angle falls as the radius grows", () => {
		expect(armAngle(0.5, 0)).toBeLessThan(armAngle(0.2, 0));
		expect(armAngle(GALAXY.armStart, 0)).toBeCloseTo(0, 10);
	});

	it("are spaced evenly around the disc", () => {
		expect(armAngle(0.4, 1) - armAngle(0.4, 0)).toBeCloseTo((2 * Math.PI) / GALAXY.arms, 10);
	});
});

describe("starCount", () => {
	it("follows the disc's on-screen area within its bounds", () => {
		expect(starCount(50)).toBe(GALAXY.minStars);
		expect(starCount(5000)).toBe(GALAXY.maxStars);
		expect(starCount(700)).toBeGreaterThan(starCount(400));
	});
});

describe("projection", () => {
	const layout = galaxyLayout(1280, 800);
	const out = new Float32Array(2);

	it("puts the core where the layout says, up and to the right of the middle", () => {
		projectInto(out, layout, 0, 1.2, 3);
		expect(out[0]).toBeCloseTo(layout.cx, 3);
		expect(out[1]).toBeCloseTo(layout.cy, 3);
		expect(layout.cx).toBeGreaterThan(640);
		expect(layout.cy).toBeLessThan(400);
	});

	it("tilts the disc: its short axis is the tilt times its long one", () => {
		let long = 0;
		let short = Infinity;
		for (let a = 0; a < Math.PI * 2; a += 0.01) {
			projectInto(out, layout, 1, a, 0);
			const d = Math.hypot(out[0] - layout.cx, out[1] - layout.cy);
			long = Math.max(long, d);
			short = Math.min(short, d);
		}
		expect(long).toBeCloseTo(layout.radius, 0);
		expect(short / long).toBeCloseTo(GALAXY.tilt, 2);
	});

	it("turns the disc as one body, once per turn period", () => {
		expect(spinAt(0)).toBe(0);
		expect(spinAt(GALAXY.turnSec * 1000)).toBeCloseTo(2 * Math.PI, 10);
		projectInto(out, layout, 0.6, 1, spinAt(0));
		const [x0, y0] = [out[0], out[1]];
		projectInto(out, layout, 0.6, 1, spinAt(GALAXY.turnSec * 1000));
		expect(out[0]).toBeCloseTo(x0, 2);
		expect(out[1]).toBeCloseTo(y0, 2);
		projectInto(out, layout, 0.6, 1, spinAt(60_000));
		expect(Math.hypot(out[0] - x0, out[1] - y0)).toBeGreaterThan(10);
	});

	it("scales with the box, so a preview tile is the window in miniature", () => {
		const small = galaxyLayout(128, 80);
		expect(small.radius / layout.radius).toBeCloseTo(0.1, 1);
		expect(small.cx / 128).toBeCloseTo(layout.cx / 1280, 10);
	});
});

describe("glow stamps", () => {
	it("are seeded and stay on the disc", () => {
		expect(armGlow(4, 100)).toEqual(armGlow(4, 100));
		for (const list of [armGlow(4, 200), dustLanes(4, 300), nebulaKnots(4, 80)]) {
			expect(list.length).toBeGreaterThan(0);
			for (const s of list) {
				expect(Math.hypot(s.x, s.y)).toBeLessThanOrEqual(1.05);
				expect(s.radius).toBeGreaterThan(0);
				expect(s.alpha).toBeGreaterThanOrEqual(0);
				expect(s.alpha).toBeLessThanOrEqual(1);
			}
		}
	});

	it("run the dust lanes ahead of the arm, the side the disc turns toward", () => {
		for (const s of dustLanes(6, 200)) {
			if (s.alpha < 0.02) continue;
			const r = Math.hypot(s.x, s.y);
			const angle = Math.atan2(s.y, s.x);
			const ahead = Math.min(
				...Array.from({ length: GALAXY.arms }, (_, arm) => {
					const d = angle - armAngle(r, arm);
					return Math.abs(Math.atan2(Math.sin(d), Math.cos(d)) - 0.042 / r);
				}),
			);
			expect(ahead).toBeLessThan(0.05);
		}
	});
});
