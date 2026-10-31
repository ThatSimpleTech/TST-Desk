// Classic's header bands (TD-5202), blurred once instead of on every repaint.
//
// Each band was a solid ellipse under CSS filter: blur(48px). Where WebKitGTK
// paints in software it worked that blur out again on every repaint of the
// header strip, and with the orbit moving the strip repaints every tick. Now
// the blur is worked out here only when the band's size or the theme
// changes, into a canvas the band carries as it drifts. It is the blur WebKit
// itself uses for CSS blur(), the SVG spec's three box passes, so a band looks
// as it did at any width: within 1/255 of the CSS blur measured in WebKitGTK.
// A radial gradient cannot do that: a blurred band three times wider than
// tall has a flat top and edges about 48 px soft on every side, while a
// gradient's falloff stretches with the band's width.

import { backingPixels, tokenColor, type Rgb } from "./paint";
import type { Scene } from "./scene";

/** The old filter: blur(48px), in CSS px. */
export const BAND_BLUR = 48;
/** How far the canvas reaches past the band on every side (2σ): beyond it the
 *  blur adds under 1/255 to the screen. */
export const BAND_PAD = 2 * BAND_BLUR;

/** The three box passes the SVG spec, and so WebKit's CSS blur, use for a
 *  Gaussian of standard deviation `sigma`: [width, offset] each, where an
 *  offset of 1 moves an even box half a pixel right of the other one. */
export function boxPasses(sigma: number): Array<[number, number]> {
	const d = Math.floor((sigma * 3 * Math.sqrt(2 * Math.PI)) / 4 + 0.5);
	if (d <= 1) return [];
	return d % 2 === 1
		? [
				[d, 0],
				[d, 0],
				[d, 0],
			]
		: [
				[d, 0],
				[d, 1],
				[d + 1, 0],
			];
}

/** One box pass along `lines` lines of `length` samples: the mean over a
 *  window `n` wide, with nothing beyond the ends (transparent, as outside
 *  the band). `step` walks along a line, `lineStep` from one to the next. */
function boxPass(
	src: Float32Array,
	dst: Float32Array,
	lines: number,
	length: number,
	step: number,
	lineStep: number,
	n: number,
	offset: number,
): void {
	const start = offset - Math.floor(n / 2);
	for (let line = 0; line < lines; line++) {
		const base = line * lineStep;
		let sum = 0;
		for (let i = Math.max(start, 0); i < Math.min(start + n, length); i++) sum += src[base + i * step];
		for (let i = 0; i < length; i++) {
			dst[base + i * step] = sum / n;
			const leaving = i + start;
			const joining = leaving + n;
			if (leaving >= 0 && leaving < length) sum -= src[base + leaving * step];
			if (joining >= 0 && joining < length) sum += src[base + joining * step];
		}
	}
}

/** How much of each pixel a solid ellipse covers, the ellipse filling the
 *  box `pad` px in from every side: exact across a row, four samples down. */
export function ellipseCoverage(width: number, height: number, pad: number): Float32Array {
	const cover = new Float32Array(width * height);
	const cx = width / 2;
	const cy = height / 2;
	const rx = cx - pad;
	const ry = cy - pad;
	if (rx <= 0 || ry <= 0) return cover;
	const SAMPLES = 4;
	for (let y = 0; y < height; y++) {
		for (let s = 0; s < SAMPLES; s++) {
			const dy = (y + (s + 0.5) / SAMPLES - cy) / ry;
			if (Math.abs(dy) >= 1) continue;
			const half = rx * Math.sqrt(1 - dy * dy);
			const left = cx - half;
			const right = cx + half;
			for (let x = Math.max(0, Math.floor(left)); x < Math.min(width, Math.ceil(right)); x++) {
				const inside = Math.min(x + 1, right) - Math.max(x, left);
				if (inside > 0) cover[y * width + x] += inside / SAMPLES;
			}
		}
	}
	return cover;
}

/** A solid ellipse filling the box `pad` px in from every side, blurred as
 *  CSS filter: blur(`sigma`) blurs it: alpha from 0 to 1, row by row. */
export function blurredEllipse(width: number, height: number, pad: number, sigma: number): Float32Array {
	let from: Float32Array = ellipseCoverage(width, height, pad);
	let to: Float32Array = new Float32Array(from.length);
	for (const [n, offset] of boxPasses(sigma)) {
		boxPass(from, to, height, width, 1, width, n, offset);
		[from, to] = [to, from];
	}
	for (const [n, offset] of boxPasses(sigma)) {
		boxPass(from, to, width, height, width, 1, n, offset);
		[from, to] = [to, from];
	}
	return from;
}

/** One header band as a Scene: its canvas covers the band's box and BAND_PAD
 *  around it, and holds the blurred band in the `token` colour at `strength`
 *  (the old colour mix times the old opacity). It moves with the band's box,
 *  so it is only painted when it appears, on a resize or a new pixel ratio
 *  (the blur is worked out again) and on a theme switch (just recoloured). */
export function bandGlowScene(token: string, strength: number): Scene {
	let pixelsWide = 1;
	let pixelsHigh = 1;
	let glow: Float32Array = new Float32Array(1);
	let image: ImageData | null = null;
	let ink: Rgb = [0, 0, 0];
	let inkAlpha = 0;
	return {
		resize(width, height, scale) {
			pixelsWide = backingPixels(width, scale);
			pixelsHigh = backingPixels(height, scale);
			const perCssPx = pixelsWide / Math.max(width, 1);
			glow = blurredEllipse(pixelsWide, pixelsHigh, BAND_PAD * perCssPx, BAND_BLUR * perCssPx);
			image = null;
		},
		recolor(read) {
			const color = tokenColor(read, token);
			ink = color.rgb;
			inkAlpha = color.alpha;
		},
		draw(ctx) {
			// Once per size: image data can only be made from a context.
			image ??= ctx.createImageData(pixelsWide, pixelsHigh);
			const data = image.data;
			const peak = 255 * strength * inkAlpha;
			for (let i = 0, p = 0; i < glow.length; i++, p += 4) {
				data[p] = ink[0];
				data[p + 1] = ink[1];
				data[p + 2] = ink[2];
				data[p + 3] = Math.round(glow[i] * peak);
			}
			ctx.putImageData(image, 0, 0);
		},
	};
}
