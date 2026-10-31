<script lang="ts">
	// Fireflies background (TD-5202): a few warm lights wander and blink
	// over a faint dusk glow along the bottom of the window. Each light is
	// two stamps of a sprite built once per theme — a soft halo and a bright
	// core — sized and faded by how lit it is. The dusk glow never moves, so
	// CSS paints it. Maths in backgrounds/fireflies.ts.
	import AmbientCanvas from "./AmbientCanvas.svelte";
	import { fireflies, fireflyInto, glowAt, type Firefly } from "../../backgrounds/fireflies";
	import { blendMode, glowSprite, tokenColor, tokenNumber } from "../../backgrounds/paint";
	import type { BackgroundProps, Scene } from "../../backgrounds/scene";

	let { animate, fill = "window" }: BackgroundProps = $props();

	const SEED = 0xf1ef1;

	let flies: Firefly[] = [];
	let width = 0;
	let height = 0;
	let blend: GlobalCompositeOperation = "source-over";
	let opacity = 1;
	let halo: HTMLCanvasElement | null = null;
	let core: HTMLCanvasElement | null = null;
	const spot = new Float32Array(2);

	const scene: Scene = {
		resize(w, h) {
			width = w;
			height = h;
			flies = fireflies(SEED, w, h);
		},
		recolor(read, doc) {
			blend = blendMode(read("--bg-ambient-blend"));
			opacity = tokenNumber(read, "--bg-firefly-opacity", 1);
			halo = glowSprite(doc, 64, tokenColor(read, "--bg-firefly-glow"), 0.08);
			core = glowSprite(doc, 16, tokenColor(read, "--bg-firefly-core"), 0.35);
		},
		draw(ctx, clockMs) {
			ctx.clearRect(0, 0, width, height);
			if (halo === null || core === null) return;
			ctx.globalCompositeOperation = blend;
			const tSec = clockMs / 1000;
			for (const fly of flies) {
				const glow = glowAt(fly, tSec);
				fireflyInto(spot, fly, tSec);
				const r = fly.radius * (0.55 + 0.45 * glow);
				ctx.globalAlpha = glow * opacity;
				ctx.drawImage(halo, spot[0] - r, spot[1] - r, r * 2, r * 2);
				const c = Math.max(1.5, fly.radius * 0.13);
				ctx.drawImage(core, spot[0] - c, spot[1] - c, c * 2, c * 2);
			}
			ctx.globalAlpha = 1;
			ctx.globalCompositeOperation = "source-over";
		},
	};
</script>

<div class="dusk" class:dusk--window={fill === "window"} aria-hidden="true"></div>
<!-- The quickest firefly wanders about 20 px a second: 24 frames is smooth. -->
<AmbientCanvas {scene} {animate} {fill} fps={24} />

<style>
	/* The last light of the evening along the bottom of the window. */
	.dusk {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: linear-gradient(to top, var(--bg-firefly-dusk), transparent 55%);
	}

	.dusk--window {
		position: fixed;
		z-index: -1;
	}
</style>
