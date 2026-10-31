<script lang="ts">
	// Aurora background (TD-5202): curtains of light rippling slowly across
	// the top of the window. An aurora is all soft edges, so the curtains are
	// drawn at a fraction of the window's resolution and the browser's
	// smoothing blurs them up — a fraction of the pixels, and no blur filter.
	// Each curtain is a row of thin strips, each strip one stretched gradient
	// sprite, so the light is brightest at the lower edge wherever that edge
	// has moved. The stars behind never move: they get their own full-
	// resolution canvas, painted once. Maths in backgrounds/aurora.ts.
	import AmbientCanvas from "./AmbientCanvas.svelte";
	import { AURORA, auroraRibbons, brightness, curtainHeight, edgeY } from "../../backgrounds/aurora";
	import { skyField } from "../../backgrounds/sky";
	import { blendMode, cssColor, tokenColor, tokenNumber, type ParsedColor } from "../../backgrounds/paint";
	import type { BackgroundProps, Scene } from "../../backgrounds/scene";

	let { animate, fill = "window" }: BackgroundProps = $props();

	const SEED = 0xa0e0;
	const SPRITE_H = 128;
	/** The lower edge sits this far down the sprite; the rest is the glow
	 *  that spills below it. */
	const EDGE_AT = 0.86;

	const ribbons = auroraRibbons(SEED);
	let width = 0;
	let height = 0;
	let blend: GlobalCompositeOperation = "source-over";
	let opacity = 1;
	let rayDepth = 0.4;
	let sprites: (HTMLCanvasElement | null)[] = [];

	function curtainSprite(doc: Document, low: ParsedColor, high: ParsedColor): HTMLCanvasElement | null {
		const sprite = doc.createElement("canvas");
		sprite.width = 1;
		sprite.height = SPRITE_H;
		const g = sprite.getContext("2d");
		if (g === null) return null;
		const gradient = g.createLinearGradient(0, 0, 0, SPRITE_H);
		gradient.addColorStop(0, cssColor(high, 0));
		gradient.addColorStop(0.3, cssColor(high, 0.3));
		gradient.addColorStop(0.66, cssColor(low, 0.62));
		gradient.addColorStop(EDGE_AT, cssColor(low, 1));
		gradient.addColorStop(1, cssColor(low, 0));
		g.fillStyle = gradient;
		g.fillRect(0, 0, 1, SPRITE_H);
		return sprite;
	}

	const curtains: Scene = {
		resize(w, h) {
			width = w;
			height = h;
		},
		recolor(read, doc) {
			blend = blendMode(read("--bg-ambient-blend"));
			opacity = tokenNumber(read, "--bg-aurora-opacity", 1);
			rayDepth = tokenNumber(read, "--bg-aurora-rays", 0.4);
			const low = tokenColor(read, "--bg-aurora-low");
			const mid = tokenColor(read, "--bg-aurora-mid");
			const high = tokenColor(read, "--bg-aurora-high");
			sprites = [curtainSprite(doc, low, high), curtainSprite(doc, mid, high), curtainSprite(doc, low, mid)];
		},
		draw(ctx, clockMs) {
			ctx.clearRect(0, 0, width, height);
			if (width <= 0) return;
			ctx.globalCompositeOperation = blend;
			const tSec = clockMs / 1000;
			const columns = Math.max(AURORA.minColumns, Math.ceil(width / AURORA.columnPx));
			const step = width / columns;
			for (const ribbon of ribbons) {
				const sprite = sprites[ribbon.tone] ?? null;
				if (sprite === null) continue;
				for (let c = 0; c < columns; c++) {
					const x01 = (c + 0.5) / columns;
					const alpha = brightness(ribbon, x01, tSec, rayDepth) * opacity;
					if (alpha <= 0.004) continue;
					const reach = curtainHeight(ribbon, x01, tSec) * height;
					const top = edgeY(ribbon, x01, tSec) * height - reach;
					ctx.globalAlpha = alpha;
					// Strips abut exactly: an overlap would add up into a bright
					// seam under the dark theme's additive blend.
					ctx.drawImage(sprite, 0, 0, 1, SPRITE_H, c * step, top, step, reach / EDGE_AT);
				}
			}
			ctx.globalAlpha = 1;
			ctx.globalCompositeOperation = "source-over";
		},
	};

	let stars = skyField({ seed: SEED, width: 0, height: 0, per100k: 0, minR: 0, maxR: 0 });
	let starWidth = 0;
	let starHeight = 0;
	let starFill = "";
	let starAlpha = 0;
	let starBlend: GlobalCompositeOperation = "source-over";

	const sky: Scene = {
		resize(w, h) {
			starWidth = w;
			starHeight = h;
			stars = skyField({ seed: SEED + 3, width: w, height: h, per100k: 18, minR: 0.3, maxR: 0.95, top: 0.7 });
		},
		recolor(read) {
			const color = tokenColor(read, "--bg-aurora-star");
			starFill = cssColor(color);
			starAlpha = color.alpha;
			starBlend = blendMode(read("--bg-ambient-blend"));
		},
		draw(ctx) {
			ctx.clearRect(0, 0, starWidth, starHeight);
			if (starAlpha <= 0) return;
			ctx.globalCompositeOperation = starBlend;
			ctx.fillStyle = starFill;
			for (const s of stars) {
				// Fainter toward the horizon, where the curtains hang.
				ctx.globalAlpha = (0.2 + 0.6 * s.bright) * (1 - (s.y / starHeight) * 0.8);
				ctx.fillRect(s.x - s.r, s.y - s.r, s.r * 2, s.r * 2);
			}
			ctx.globalAlpha = 1;
			ctx.globalCompositeOperation = "source-over";
		},
	};
</script>

<div class="sky" class:sky--window={fill === "window"} aria-hidden="true"></div>
<AmbientCanvas scene={sky} animate={false} {fill} />
<!-- A preview tile is too small to draw at a fraction: it would show its pixels. -->
<AmbientCanvas scene={curtains} {animate} {fill} resolution={fill === "window" ? 0.3 : 1} fps={AURORA.fps} />

<style>
	/* A faint wash of the aurora's colour at the top of the sky. Static, so
	   CSS paints it. */
	.sky {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: linear-gradient(to bottom, var(--bg-aurora-sky), transparent 62%);
	}

	.sky--window {
		position: fixed;
		z-index: -1;
	}
</style>
