<script lang="ts">
	// Constellations background (TD-5202): stars wander slowly and join up
	// with faint lines when they come near, under a faint deepening of the
	// sky that CSS paints once. Lines are batched into a few
	// opacity steps, one path and one stroke per step, so a frame draws a
	// handful of strokes however many links there are. Maths in
	// backgrounds/constellations.ts.
	import AmbientCanvas from "./AmbientCanvas.svelte";
	import { collectLinks, drifters, linkReach, positionsInto, type Drifter } from "../../backgrounds/constellations";
	import { twinkleAlpha } from "../../backgrounds/sky";
	import { blendMode, cssColor, glowSprite, tokenColor, tokenNumber } from "../../backgrounds/paint";
	import type { BackgroundProps, Scene } from "../../backgrounds/scene";

	let { animate, fill = "window" }: BackgroundProps = $props();

	const SEED = 0xc0457;
	/** Opacity steps the lines are batched into. */
	const STEPS = 4;
	const MAX_LINKS = 480;
	/** Stars brighter than this get a halo. */
	const HALO_FROM = 0.55;

	let stars: Drifter[] = [];
	let xs = new Float32Array(0);
	let ys = new Float32Array(0);
	const pairs = new Uint16Array(MAX_LINKS * 2);
	const alphas = new Float32Array(MAX_LINKS);
	let width = 0;
	let height = 0;
	let reach = 0;
	let blend: GlobalCompositeOperation = "source-over";
	let starFill = "";
	let brightFill = "";
	let lineStroke = "";
	let opacity = 1;
	let halo: HTMLCanvasElement | null = null;

	const scene: Scene = {
		resize(w, h) {
			width = w;
			height = h;
			// Faintest first: the bright ones paint on top, and the fill
			// colour changes once a frame, not once a star.
			stars = drifters(SEED, w, h).sort((a, b) => a.bright - b.bright);
			xs = new Float32Array(stars.length);
			ys = new Float32Array(stars.length);
			reach = linkReach(w, h, stars.length);
		},
		recolor(read, doc) {
			blend = blendMode(read("--bg-ambient-blend"));
			opacity = tokenNumber(read, "--bg-constellation-opacity", 1);
			starFill = cssColor(tokenColor(read, "--bg-constellation-star"));
			const bright = tokenColor(read, "--bg-constellation-bright");
			brightFill = cssColor(bright);
			lineStroke = cssColor(tokenColor(read, "--bg-constellation-line"));
			halo = glowSprite(doc, 32, bright, 0.12);
		},
		draw(ctx, clockMs) {
			ctx.clearRect(0, 0, width, height);
			ctx.globalCompositeOperation = blend;
			positionsInto(xs, ys, stars, clockMs / 1000, width, height, reach);
			const links = collectLinks(xs, ys, stars.length, reach, pairs, alphas);
			ctx.strokeStyle = lineStroke;
			ctx.lineWidth = 0.9;
			for (let step = 0; step < STEPS; step++) {
				const lo = step / STEPS;
				const hi = (step + 1) / STEPS;
				ctx.beginPath();
				let any = false;
				for (let k = 0; k < links; k++) {
					const a = alphas[k];
					if (a <= lo || a > hi) continue;
					const i = pairs[k * 2];
					const j = pairs[k * 2 + 1];
					ctx.moveTo(xs[i], ys[i]);
					ctx.lineTo(xs[j], ys[j]);
					any = true;
				}
				if (!any) continue;
				ctx.globalAlpha = ((lo + hi) / 2) * 0.7 * opacity;
				ctx.stroke();
			}
			ctx.fillStyle = starFill;
			for (let i = 0; i < stars.length; i++) {
				const s = stars[i];
				const twinkle = twinkleAlpha(s, clockMs);
				if (s.bright >= HALO_FROM && halo !== null) {
					const r = s.size * 2.6;
					ctx.globalAlpha = twinkle * opacity;
					ctx.drawImage(halo, xs[i] - r, ys[i] - r, r * 2, r * 2);
					continue;
				}
				if (s.bright > 0.3) ctx.fillStyle = brightFill;
				ctx.globalAlpha = (0.45 + 0.55 * twinkle) * opacity;
				ctx.fillRect(xs[i] - s.size / 2, ys[i] - s.size / 2, s.size, s.size);
			}
			ctx.globalAlpha = 1;
			ctx.globalCompositeOperation = "source-over";
		},
	};
</script>

<div class="night" class:night--window={fill === "window"} aria-hidden="true"></div>
<!-- Stars drift a few pixels a second: 20 frames is smooth. -->
<AmbientCanvas {scene} {animate} {fill} fps={20} />

<style>
	/* A faint deepening of the sky behind the stars. Static: CSS paints it. */
	.night {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: radial-gradient(ellipse 85% 75% at 30% 18%, var(--bg-constellation-sky), transparent 78%);
	}

	.night--window {
		position: fixed;
		z-index: -1;
	}
</style>
