// Classic's header bands, blurred once (TD-5202): the blur is the SVG spec's
// three box passes, which is what WebKit draws for CSS filter: blur(), so a
// band matches the old CSS at any width; the scene paints it in its token's
// colour at the old strength and blurs again only on a resize.

import { describe, expect, it } from "vitest";
import { BAND_BLUR, BAND_PAD, bandGlowScene, blurredEllipse, boxPasses, ellipseCoverage } from "./band-glow";

/** WebKitGTK 2.52 drawing a white 615×200 ellipse under filter: blur(48px),
 *  inset 96 px in an 807×392 box on black (screenshot grey levels): its
 *  middle row every 60 px, and its middle column every 30 px. */
const CSS_ROW = [1, 31, 120, 204, 236, 246, 249, 249, 247, 242, 223, 163, 64, 7];
const CSS_COLUMN = [3, 19, 56, 113, 175, 223, 246, 247, 224, 177, 115, 57, 20, 4];

describe("boxPasses", () => {
	it("splits a 48 px blur into two 90 px boxes half a pixel apart and one of 91", () => {
		expect(boxPasses(48)).toEqual([
			[90, 0],
			[90, 1],
			[91, 0],
		]);
	});

	it("uses three equal boxes when the width comes out odd", () => {
		expect(boxPasses(24)).toEqual([
			[45, 0],
			[45, 0],
			[45, 0],
		]);
	});

	it("does nothing for a blur under a pixel", () => {
		expect(boxPasses(0.3)).toEqual([]);
	});
});

describe("blurredEllipse", () => {
	const W = 807;
	const H = 392;
	const glow = blurredEllipse(W, H, BAND_PAD, BAND_BLUR);
	const at = (x: number, y: number): number => glow[y * W + x];

	it("matches the CSS blur it replaces, as WebKitGTK drew it", () => {
		CSS_ROW.forEach((grey, i) => expect(Math.abs(at(i * 60, 196) * 255 - grey), `x ${i * 60}`).toBeLessThanOrEqual(1.5));
		CSS_COLUMN.forEach((grey, i) => expect(Math.abs(at(403, i * 30) * 255 - grey), `y ${i * 30}`).toBeLessThanOrEqual(1.5));
	});

	it("is the same on both sides of each axis", () => {
		for (const [x, y] of [
			[60, 196],
			[200, 120],
			[350, 40],
		]) {
			expect(at(x, y)).toBeCloseTo(at(W - 1 - x, y), 2);
			expect(at(x, y)).toBeCloseTo(at(x, H - 1 - y), 2);
		}
	});

	it("keeps nearly all of the ellipse inside the padded box", () => {
		const sum = (values: Float32Array): number => values.reduce((total, v) => total + v, 0);
		const solid = sum(ellipseCoverage(W, H, BAND_PAD));
		expect(solid).toBeCloseTo(Math.PI * 307.5 * 100, -2);
		expect(sum(glow) / solid).toBeGreaterThan(0.99);
	});
});

describe("bandGlowScene", () => {
	/** A context that hands out image data and keeps what is put back. */
	function recorder() {
		const put: ImageData[] = [];
		const ctx = {
			createImageData: (w: number, h: number) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
			putImageData: (image: ImageData) => put.push(image),
		} as unknown as CanvasRenderingContext2D;
		return { ctx, put };
	}

	const tokens: Record<string, string> = { "--color-accent": "#b4532a" };
	const read = (name: string): string => tokens[name] ?? "";
	/** The scene reads only the tokens. */
	const doc = {} as Document;

	function centre(image: ImageData): number[] {
		const i = (Math.floor(image.height / 2) * image.width + Math.floor(image.width / 2)) * 4;
		return [...image.data.slice(i, i + 4)];
	}

	it("paints the band in its token's colour at its strength", () => {
		const scene = bandGlowScene("--color-accent", 0.72 * 0.42);
		const { ctx, put } = recorder();
		scene.resize(807, 392, 1);
		scene.recolor(read, doc);
		scene.draw(ctx, 0);
		expect([put[0].width, put[0].height]).toEqual([807, 392]);
		const [r, g, b, a] = centre(put[0]);
		expect([r, g, b]).toEqual([180, 83, 42]);
		expect(a).toBeCloseTo((249 / 255) * 0.72 * 0.42 * 255, -0.5);
	});

	it("draws at the canvas's own pixel size, the same band at half resolution", () => {
		const scene = bandGlowScene("--color-accent", 0.72 * 0.42);
		const { ctx, put } = recorder();
		scene.resize(807, 392, 0.5);
		scene.recolor(read, doc);
		scene.draw(ctx, 0);
		expect([put[0].width, put[0].height]).toEqual([404, 196]);
		expect(centre(put[0])[3]).toBeCloseTo((249 / 255) * 0.72 * 0.42 * 255, -0.5);
	});

	it("recolours on a theme switch without blurring again", () => {
		const scene = bandGlowScene("--color-accent", 0.5);
		const { ctx, put } = recorder();
		scene.resize(400, 300, 1);
		scene.recolor(read, doc);
		scene.draw(ctx, 0);
		const alpha = centre(put[0])[3];
		tokens["--color-accent"] = "#e07a4f";
		scene.recolor(read, doc);
		scene.draw(ctx, 0);
		expect(centre(put[1])).toEqual([224, 122, 79, alpha]);
		// The same image data is filled again: nothing allocated per paint.
		expect(put[1]).toBe(put[0]);
		tokens["--color-accent"] = "#b4532a";
	});
});
