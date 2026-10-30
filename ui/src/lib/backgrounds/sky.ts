// A field of faint, twinkling background stars (TD-5202), shared by the
// space backgrounds. It is the Classic dust's speck and twinkle (TD-1750)
// with its own density and size range, so every sky shimmers the same way.

import { twinkleAlpha, type Dust } from "../playful-bg";
import { mulberry32 } from "./random";

export interface SkyOptions {
	seed: number;
	width: number;
	height: number;
	/** Stars per 100k CSS px². */
	per100k: number;
	minR: number;
	maxR: number;
	/** Fill only the top part of the box, 0–1 of its height (aurora skies). */
	top?: number;
}

export function skyField(opts: SkyOptions): Dust[] {
	const { width, height } = opts;
	if (width <= 0 || height <= 0) return [];
	const reach = Math.min(1, Math.max(0, opts.top ?? 1));
	const rand = mulberry32(opts.seed);
	const count = Math.round(((width * height * reach) / 100_000) * opts.per100k);
	const stars: Dust[] = [];
	for (let i = 0; i < count; i++) {
		stars.push({
			x: rand() * width,
			y: rand() * height * reach,
			r: opts.minR + rand() * (opts.maxR - opts.minR),
			phase: rand(),
			cps: 0.03 + rand() * 0.1,
			bright: rand() * rand(),
		});
	}
	return stars;
}

export { twinkleAlpha };
