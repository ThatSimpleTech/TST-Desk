<script lang="ts">
	// Deep space background (TD-5202): a parallax starfield drifting toward
	// the viewer, over a far layer of specks that only twinkle, with the odd
	// shooting star. The faint haze never moves, so it is a CSS gradient —
	// composited once, free on every frame — and only the stars use the
	// canvas. Maths in backgrounds/deep-space.ts.
	import AmbientCanvas from "./AmbientCanvas.svelte";
	import {
		meteorInto,
		projectInto,
		SPACE,
		SPACE_TONES,
		spaceField,
		spaceLayout,
		starInto,
	} from "../../backgrounds/deep-space";
	import { skyField, twinkleAlpha } from "../../backgrounds/sky";
	import { blendMode, cssColor, glowSprite, tokenColor, tokenNumber } from "../../backgrounds/paint";
	import type { BackgroundProps, Scene } from "../../backgrounds/scene";

	let { animate, fill = "window" }: BackgroundProps = $props();

	const SEED = 0xd5ace;
	/** Stars this big get a soft halo instead of a square. */
	const HALO_FROM = 1.25;
	const TAIL_STEPS = 12;
	const TONE_TOKENS = ["--bg-space-star", "--bg-space-cool", "--bg-space-warm"];

	let width = 0;
	let height = 0;
	let layout = spaceLayout(0, 0);
	let field = spaceField(SEED, 0, 0);
	let far = skyField({ seed: SEED, width: 0, height: 0, per100k: 0, minR: 0, maxR: 0 });
	let blend: GlobalCompositeOperation = "source-over";
	let toneFill: string[] = [];
	let farFill = "";
	let meteorFill = "";
	let opacity = 1;
	let halos: (HTMLCanvasElement | null)[] = [];
	let meteorHead: HTMLCanvasElement | null = null;
	const star = new Float32Array(3);
	const spot = new Float32Array(4);
	const meteor = new Float32Array(6);

	function drawMeteor(ctx: CanvasRenderingContext2D): void {
		const reach = Math.max(width, height);
		const length = meteor[5] * reach;
		const progress = meteor[4];
		const fade = Math.sin(Math.PI * progress);
		const headX = meteor[0] * width + meteor[2] * length * 2.2 * progress;
		const headY = meteor[1] * height + meteor[3] * length * 2.2 * progress;
		const tail = length * (0.35 + 0.65 * fade);
		ctx.strokeStyle = meteorFill;
		ctx.lineCap = "round";
		for (let k = 0; k < TAIL_STEPS; k++) {
			const a = k / TAIL_STEPS;
			const b = (k + 1) / TAIL_STEPS;
			ctx.globalAlpha = fade * (1 - a) ** 1.6 * 0.85 * opacity;
			ctx.lineWidth = 1.7 * (1 - a) + 0.3;
			ctx.beginPath();
			ctx.moveTo(headX - meteor[2] * tail * a, headY - meteor[3] * tail * a);
			ctx.lineTo(headX - meteor[2] * tail * b, headY - meteor[3] * tail * b);
			ctx.stroke();
		}
		if (meteorHead !== null) {
			ctx.globalAlpha = fade * opacity;
			ctx.drawImage(meteorHead, headX - 6, headY - 6, 12, 12);
		}
	}

	const scene: Scene = {
		resize(w, h) {
			width = w;
			height = h;
			layout = spaceLayout(w, h);
			field = spaceField(SEED, w, h);
			far = skyField({ seed: SEED + 5, width: w, height: h, per100k: 12, minR: 0.3, maxR: 0.8 });
		},
		recolor(read, doc) {
			blend = blendMode(read("--bg-ambient-blend"));
			opacity = tokenNumber(read, "--bg-space-opacity", 1);
			const tones = TONE_TOKENS.map((name) => tokenColor(read, name));
			toneFill = tones.map((c) => cssColor(c));
			halos = tones.map((c) => glowSprite(doc, 32, c, 0.14));
			farFill = cssColor(tokenColor(read, "--bg-space-dust"));
			const streak = tokenColor(read, "--bg-space-meteor");
			meteorFill = cssColor(streak);
			meteorHead = glowSprite(doc, 24, streak, 0.2);
		},
		draw(ctx, clockMs) {
			ctx.clearRect(0, 0, width, height);
			ctx.globalCompositeOperation = blend;
			const tSec = clockMs / 1000;
			ctx.fillStyle = farFill;
			for (const s of far) {
				ctx.globalAlpha = twinkleAlpha(s, clockMs) * 0.7 * opacity;
				ctx.fillRect(s.x - s.r, s.y - s.r, s.r * 2, s.r * 2);
			}
			for (let tone = 0; tone < SPACE_TONES; tone++) {
				ctx.fillStyle = toneFill[tone] ?? farFill;
				const halo = halos[tone] ?? null;
				for (let i = field.toneStart[tone]; i < field.toneStart[tone + 1]; i++) {
					starInto(star, field, layout, i, tSec);
					projectInto(spot, layout, star[0], star[1], star[2], field.size[i]);
					const x = spot[0];
					const y = spot[1];
					const r = spot[2];
					if (x < -10 || y < -10 || x > width + 10 || y > height + 10) continue;
					ctx.globalAlpha = spot[3] * opacity;
					if (r >= HALO_FROM && halo !== null) ctx.drawImage(halo, x - r * 2.5, y - r * 2.5, r * 5, r * 5);
					else ctx.fillRect(x - r, y - r, r * 2, r * 2);
				}
			}
			if (meteorInto(meteor, SEED, tSec)) drawMeteor(ctx);
			ctx.globalAlpha = 1;
			ctx.globalCompositeOperation = "source-over";
		},
	};
</script>

<div
	class="haze"
	class:haze--window={fill === "window"}
	style:--space-vx="{SPACE.vanishX * 100}%"
	style:--space-vy="{SPACE.vanishY * 100}%"
	aria-hidden="true"
></div>
<AmbientCanvas {scene} {animate} {fill} />

<style>
	/* The light of the deep field: a glow where the stars come from and a
	   faint band across the sky. Static, so CSS draws it, not the canvas. */
	.haze {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background:
			radial-gradient(ellipse 48% 42% at var(--space-vx) var(--space-vy), var(--bg-space-core), transparent 72%),
			linear-gradient(155deg, transparent 22%, var(--bg-space-haze) 48%, transparent 76%);
	}

	.haze--window {
		position: fixed;
		z-index: -1;
	}
</style>
