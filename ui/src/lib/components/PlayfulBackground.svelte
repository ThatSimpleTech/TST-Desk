<script lang="ts">
	// Playful background (TD-1750): a field of fine dust that twinkles
	// slowly and drifts upward — sparse light specks on the ground colour,
	// the Claude Code starfield look. Pure maths lives in playful-bg.ts.
	//
	// Since TD-5202 it is Classic's dust inside the chat pane, painted
	// through the shared AmbientCanvas (frame cap, paused while hidden, a
	// still frame when motion is off) instead of its own loop. The click
	// ripples it used to draw are RippleLayer's now, shared by every
	// background, and look the same. Specks read the ink tokens so they stay
	// neutral on both grounds; the accent stays reserved for ripples.
	import AmbientCanvas from "./backgrounds/AmbientCanvas.svelte";
	import { driftY, dustField, twinkleAlpha, type Dust } from "../playful-bg";
	import { cssColor, tokenColor } from "../backgrounds/paint";
	import type { BackgroundProps, Scene } from "../backgrounds/scene";

	let { animate, fill = "parent" }: BackgroundProps = $props();

	/** The brightest specks read in ink (white on dark), the quiet majority
	 *  in its secondary — the reference field is neutral, not accent. */
	const isBright = (s: Dust): boolean => s.bright > 0.75;

	let specks: Dust[] = [];
	/** specks[0, split) are quiet, the rest bright: one colour switch a frame. */
	let split = 0;
	let width = 0;
	let height = 0;
	let ink = "";
	let inkSoft = "";

	const scene: Scene = {
		resize(w, h) {
			width = w;
			height = h;
			specks = dustField({ seed: 0x5eed, width: w, height: h }).sort(
				(a, b) => Number(isBright(a)) - Number(isBright(b)),
			);
			split = specks.findIndex(isBright);
			if (split < 0) split = specks.length;
		},
		recolor(read) {
			ink = cssColor(tokenColor(read, "--color-ink"));
			const soft = tokenColor(read, "--color-ink-secondary");
			inkSoft = soft.alpha > 0 ? cssColor(soft) : ink;
		},
		draw(ctx, clockMs) {
			ctx.clearRect(0, 0, width, height);
			const tSec = clockMs / 1000;
			ctx.fillStyle = inkSoft;
			for (let i = 0; i < specks.length; i++) {
				const s = specks[i];
				if (i === split) ctx.fillStyle = ink;
				ctx.globalAlpha = twinkleAlpha(s, clockMs);
				ctx.beginPath();
				ctx.arc(s.x, driftY(s.y, tSec, height), s.r, 0, Math.PI * 2);
				ctx.fill();
			}
			ctx.globalAlpha = 1;
		},
	};
</script>

<AmbientCanvas {scene} {animate} {fill} />
