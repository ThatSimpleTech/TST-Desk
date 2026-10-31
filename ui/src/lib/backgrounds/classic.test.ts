// Classic's motion on one clock (TD-5202): the curves are the old CSS
// keyframes and timing functions, so the look at any moment is what the CSS
// drew then; the bands ease with the --ease-out token; the tick is fine
// enough that the outer ring steps about 4 px; and no CSS animation,
// transition, blur filter or full-window opacity is left in Classic's
// components to repaint the window on frames of its own.

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { CLASSIC_FPS, EASE_OUT_CURVE, bandDrift, cubicBezier, orbitPose } from "./classic";

const source = (path: string): string => readFileSync(resolve(process.cwd(), path), "utf-8");

describe("cubicBezier", () => {
	it("matches the CSS timing functions", () => {
		expect(cubicBezier(0.25, 0.1, 0.25, 1)(0.5)).toBeCloseTo(0.8024034, 5);
		expect(cubicBezier(0.42, 0, 0.58, 1)(0.25)).toBeCloseTo(0.1291619, 5);
		expect(cubicBezier(0.42, 0, 0.58, 1)(0.5)).toBeCloseTo(0.5, 6);
		expect(cubicBezier(...EASE_OUT_CURVE)(0.25)).toBeCloseTo(0.7273734, 5);
		expect(cubicBezier(...EASE_OUT_CURVE)(0.5)).toBeCloseTo(0.9295788, 5);
		expect(cubicBezier(0, 0, 1, 1)(0.3)).toBeCloseTo(0.3, 6);
	});

	it("is pinned at both ends", () => {
		const ease = cubicBezier(...EASE_OUT_CURVE);
		expect(ease(0)).toBe(0);
		expect(ease(1)).toBe(1);
		expect(ease(-0.5)).toBe(0);
		expect(ease(2)).toBe(1);
	});
});

describe("orbitPose", () => {
	it("starts where the old keyframes started", () => {
		const pose = orbitPose(0);
		expect(pose.turnDeg).toBe(0);
		expect(pose.scale).toBe(1);
		expect(pose.wash).toBeCloseTo(0.75, 6);
		expect(pose.glow).toBeCloseTo(0.7, 6);
		expect(pose.anchor).toBeCloseTo(0.7, 6);
	});

	it("turns once in 80 s, swelling to 1.02 half way round", () => {
		expect(orbitPose(20_000).turnDeg).toBeCloseTo(90, 6);
		expect(orbitPose(20_000).scale).toBeCloseTo(1.01, 6);
		expect(orbitPose(40_000).turnDeg).toBeCloseTo(180, 6);
		expect(orbitPose(40_000).scale).toBeCloseTo(1.02, 6);
		expect(orbitPose(80_000).turnDeg).toBeCloseTo(0, 6);
		expect(orbitPose(80_000).scale).toBeCloseTo(1, 6);
	});

	it("breathes the wash over 14 s, the dots over 9 s and the anchor over 6 s, eased in and out", () => {
		expect(orbitPose(7_000).wash).toBeCloseTo(1, 6);
		expect(orbitPose(14_000).wash).toBeCloseTo(0.75, 6);
		// A quarter of the way: half way up the eased rise.
		expect(orbitPose(3_500).wash).toBeCloseTo(0.875, 6);
		expect(orbitPose(1_750).wash).toBeCloseTo(0.75 + 0.25 * 0.1291619, 5);
		expect(orbitPose(4_500).glow).toBeCloseTo(1, 6);
		expect(orbitPose(9_000).glow).toBeCloseTo(0.7, 6);
		expect(orbitPose(3_000).anchor).toBeCloseTo(1, 6);
		expect(orbitPose(4_500).anchor).toBeCloseTo(0.85, 6);
	});

	it("moves without a jump anywhere along the clock", () => {
		let last = orbitPose(0);
		for (let t = 10; t <= 200_000; t += 10) {
			const pose = orbitPose(t);
			const turn = Math.abs(pose.turnDeg - last.turnDeg);
			expect(Math.min(turn, 360 - turn)).toBeLessThan(0.05);
			expect(Math.abs(pose.scale - last.scale)).toBeLessThan(1e-4);
			expect(Math.abs(pose.wash - last.wash)).toBeLessThan(0.002);
			expect(Math.abs(pose.glow - last.glow)).toBeLessThan(0.002);
			expect(Math.abs(pose.anchor - last.anchor)).toBeLessThan(0.002);
			last = pose;
		}
	});
});

describe("bandDrift", () => {
	it("swings the accent band out and back over 18 s each way, starting at home", () => {
		expect(bandDrift(0).accent).toBe(0);
		expect(bandDrift(4_500).accent).toBeCloseTo(0.7273734, 5);
		expect(bandDrift(18_000).accent).toBeCloseTo(1, 6);
		// On the way back the curve runs backwards: it leaves the far end slowly.
		expect(bandDrift(18_000 + 4_500).accent).toBeCloseTo(cubicBezier(...EASE_OUT_CURVE)(0.75), 6);
		expect(bandDrift(36_000).accent).toBeCloseTo(0, 6);
	});

	it("swings the warm band over 26 s each way, starting at the far end (alternate-reverse)", () => {
		expect(bandDrift(0).warm).toBe(1);
		expect(bandDrift(26_000).warm).toBeCloseTo(0, 6);
		expect(bandDrift(52_000).warm).toBeCloseTo(1, 6);
	});

	it("moves without a jump anywhere along the clock", () => {
		let last = bandDrift(0);
		for (let t = 10; t <= 200_000; t += 10) {
			const drift = bandDrift(t);
			// The steepest the ease-out curve gets is 3.5 times linear.
			expect(Math.abs(drift.accent - last.accent)).toBeLessThan(0.0025);
			expect(Math.abs(drift.warm - last.warm)).toBeLessThan(0.0025);
			last = drift;
		}
	});

	it("eases with the --ease-out token the bands always used", () => {
		const css = source("src/lib/tokens.css");
		const token = /--ease-out:\s*cubic-bezier\(([^)]*)\)/.exec(css);
		expect(token).not.toBeNull();
		expect(token?.[1].split(",").map(Number)).toEqual([...EASE_OUT_CURVE]);
	});
});

describe("the tick", () => {
	it("steps the outermost ring about 4 px at a time in a 1200×800 window", () => {
		// The field is the window grown 8% on every side; the 1200×1040 view
		// box covers it, so a view-box unit is 1.16 px, and the outer ring
		// sits 525 units out and turns once in 80 s.
		const radiusPx = 525 * Math.max((1200 * 1.16) / 1200, (800 * 1.16) / 1040);
		const stepPx = (2 * Math.PI * radiusPx) / 80 / CLASSIC_FPS;
		expect(stepPx).toBeLessThanOrEqual(4.5);
		// And no faster than it needs: every tick repaints the window.
		expect(CLASSIC_FPS).toBeLessThanOrEqual(15);
	});
});

describe("Classic's components", () => {
	const FILES = [
		"src/lib/BackgroundOrbit.svelte",
		"src/lib/components/backgrounds/ClassicChatLayer.svelte",
		"src/lib/components/PlayfulBackground.svelte",
	];

	/** The component's rules, without comments (they may name the old way). */
	function styles(path: string): string {
		const css = /<style>([\s\S]*?)<\/style>/.exec(source(path))?.[1] ?? "";
		return css.replace(/\/\*[\s\S]*?\*\//g, "");
	}

	it("keep no CSS animation or transition, which would repaint on frames of their own", () => {
		for (const path of FILES) {
			const css = styles(path);
			expect(css, path).not.toMatch(/animation/);
			expect(css, path).not.toMatch(/@keyframes/);
			expect(css, path).not.toMatch(/transition/);
		}
	});

	it("blur nothing as they paint: the bands are blurred in advance", () => {
		for (const path of FILES) expect(styles(path), path).not.toMatch(/filter:\s*blur/);
	});

	it("lay no group opacity over the whole window", () => {
		const orbit = /\.orbit\s*\{([^}]*)\}/.exec(styles("src/lib/BackgroundOrbit.svelte"))?.[1] ?? "";
		expect(orbit).toContain("position: fixed");
		expect(orbit).not.toMatch(/opacity/);
		expect(source("src/lib/tokens.css")).not.toContain("--bg-orbit-opacity");
	});
});
