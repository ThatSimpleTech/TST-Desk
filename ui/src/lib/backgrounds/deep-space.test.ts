// Deep space maths (TD-5202): stars that come toward the viewer and start
// again somewhere new, a projection that grows and brightens them as they
// come, and shooting stars on a seeded schedule — all pure functions of
// time.

import { describe, expect, it } from "vitest";
import {
	SPACE,
	SPACE_TONES,
	meteorInto,
	projectInto,
	spaceField,
	spaceLayout,
	starInto,
	starTotal,
} from "./deep-space";

const W = 1280;
const H = 800;
const layout = spaceLayout(W, H);
const field = spaceField(42, W, H);
const star = new Float32Array(3);
const spot = new Float32Array(4);

describe("spaceField", () => {
	it("is deterministic for a seed and box", () => {
		expect(spaceField(42, W, H)).toEqual(field);
		expect(spaceField(43, W, H).offset).not.toEqual(field.offset);
	});

	it("scales its star count with the box, within bounds", () => {
		expect(starTotal(0, 100)).toBe(0);
		expect(starTotal(120, 75)).toBe(SPACE.minStars);
		expect(starTotal(8000, 6000)).toBe(SPACE.maxStars);
		expect(starTotal(1920, 1080)).toBeGreaterThan(starTotal(1280, 800));
	});

	it("sorts stars by tone so a frame sets each colour once", () => {
		for (let i = 1; i < field.count; i++) expect(field.tone[i]).toBeGreaterThanOrEqual(field.tone[i - 1]);
		expect(field.toneStart[SPACE_TONES]).toBe(field.count);
	});
});

describe("starInto", () => {
	it("keeps depth between near and far", () => {
		for (let i = 0; i < field.count; i += 7) {
			for (const t of [0, 3.3, 47, 1000, 86_400]) {
				starInto(star, field, layout, i, t);
				expect(star[2]).toBeGreaterThan(SPACE.near - 1e-6);
				expect(star[2]).toBeLessThanOrEqual(1);
			}
		}
	});

	it("brings a star closer as time passes", () => {
		starInto(star, field, layout, 3, 10);
		const z0 = star[2];
		const x0 = star[0];
		starInto(star, field, layout, 3, 10.5);
		// Same trip (unless it just wrapped): nearer, same heading.
		if (star[0] === x0) expect(star[2]).toBeLessThan(z0);
		expect(Math.abs(star[2] - z0)).toBeLessThan(0.05);
	});

	it("starts a star again somewhere new each trip", () => {
		const trip = (1 - SPACE.near) / SPACE.speed;
		starInto(star, field, layout, 5, 1);
		const first = [star[0], star[1]];
		starInto(star, field, layout, 5, 1 + trip);
		expect([star[0], star[1]]).not.toEqual(first);
		starInto(star, field, layout, 5, 1 + trip * 0.25);
		expect(star[0]).toBe(first[0]);
	});

	it("lands every trip inside the window at the landing depth", () => {
		for (let i = 0; i < field.count; i++) {
			starInto(star, field, layout, i, 12.5);
			projectInto(spot, layout, star[0], star[1], SPACE.landing, 1);
			expect(spot[0]).toBeGreaterThanOrEqual(-W * SPACE.overscan - 1);
			expect(spot[0]).toBeLessThanOrEqual(W * (1 + SPACE.overscan) + 1);
			expect(spot[1]).toBeGreaterThanOrEqual(-H * SPACE.overscan - 1);
			expect(spot[1]).toBeLessThanOrEqual(H * (1 + SPACE.overscan) + 1);
		}
	});
});

describe("projectInto", () => {
	it("streams out of the vanishing point", () => {
		projectInto(spot, layout, 0, 0, 0.5, 1);
		// Float32 output: three decimals is a thousandth of a pixel.
		expect(spot[0]).toBeCloseTo(W * SPACE.vanishX, 3);
		expect(spot[1]).toBeCloseTo(H * SPACE.vanishY, 3);
		projectInto(spot, layout, 0.1, 0, 1, 1);
		const far = spot[0] - layout.vx;
		projectInto(spot, layout, 0.1, 0, 0.5, 1);
		expect(spot[0] - layout.vx).toBeCloseTo(far * 2, 3);
	});

	it("grows and brightens a star as it nears, and fades a far one in", () => {
		projectInto(spot, layout, 0, 0, 0.9, 1);
		const [farR, farA] = [spot[2], spot[3]];
		projectInto(spot, layout, 0, 0, 0.2, 1);
		expect(spot[2]).toBeGreaterThan(farR);
		expect(spot[3]).toBeGreaterThan(farA);
		projectInto(spot, layout, 0, 0, 1, 1);
		expect(spot[3]).toBe(0);
		for (const z of [1, 0.8, 0.5, 0.2, SPACE.near]) {
			projectInto(spot, layout, 0, 0, z, 1.4);
			expect(spot[3]).toBeGreaterThanOrEqual(0);
			expect(spot[3]).toBeLessThanOrEqual(1);
		}
	});

	it("draws smaller stars in a small box", () => {
		const tile = spaceLayout(120, 75);
		projectInto(spot, tile, 0, 0, 0.3, 1);
		const tileR = spot[2];
		projectInto(spot, layout, 0, 0, 0.3, 1);
		expect(tileR).toBeLessThan(spot[2]);
	});
});

describe("meteorInto", () => {
	const meteor = new Float32Array(6);

	it("is clear most of the time and flies now and then", () => {
		let flying = 0;
		const samples = 20_000;
		for (let k = 0; k < samples; k++) if (meteorInto(meteor, 7, k * 0.1)) flying++;
		const share = flying / samples;
		const expected = (SPACE.meteorChance * SPACE.meteorSec) / SPACE.meteorSlotSec;
		expect(share).toBeGreaterThan(expected * 0.6);
		expect(share).toBeLessThan(expected * 1.4);
	});

	it("is deterministic and well formed while it flies", () => {
		let seen = 0;
		for (let k = 0; k < 4000 && seen < 20; k++) {
			const t = k * 0.25;
			if (!meteorInto(meteor, 9, t)) continue;
			seen++;
			const copy = Float32Array.from(meteor);
			expect(meteorInto(meteor, 9, t)).toBe(true);
			expect(meteor).toEqual(copy);
			expect(Math.hypot(meteor[2], meteor[3])).toBeCloseTo(1, 5);
			expect(meteor[3]).toBeGreaterThan(0);
			expect(meteor[4]).toBeGreaterThanOrEqual(0);
			expect(meteor[4]).toBeLessThanOrEqual(1);
			expect(meteor[0]).toBeGreaterThan(0);
			expect(meteor[0]).toBeLessThan(1);
		}
		expect(seen).toBe(20);
	});

	it("never flies before time starts", () => {
		expect(meteorInto(meteor, 1, -5)).toBe(false);
	});
});
