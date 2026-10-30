<script lang="ts">
	// The canvas every drawn background paints into (TD-5202). It owns what
	// they would otherwise each repeat: sizing the backing store to the box
	// at a capped pixel ratio (lower behind the whole window than in a
	// preview tile), the shared frame loop (frame cap, paused while the
	// document is hidden, nothing scheduled when still), repainting on a
	// resize or a move to a screen of another density, and recolouring on a
	// theme switch. A background hands it a Scene and decides nothing else
	// about frames.
	//
	// Canvas 2D only: some machines this runs on have no WebGL.
	import { untrack } from "svelte";
	import { createLoop, windowFrameHost, type Loop } from "../../backgrounds/loop";
	import {
		DPR_CAP,
		WINDOW_DPR_CAP,
		canvasScale,
		fitCanvas,
		tokenReader,
		watchPixelRatio,
		watchTheme,
	} from "../../backgrounds/paint";
	import type { Scene } from "../../backgrounds/scene";

	interface Props {
		scene: Scene;
		animate: boolean;
		fill?: "window" | "parent";
		/** Backing-store pixels relative to the capped device ratio. Soft
		 *  scenes draw small and let the browser's smoothing blur them up. */
		resolution?: number;
		/** Most frames a second; null for the display's own rate. */
		fps?: number | null;
	}

	let { scene, animate, fill = "window", resolution = 1, fps = 30 }: Props = $props();

	let host: HTMLDivElement | undefined = $state();
	let canvas: HTMLCanvasElement | undefined = $state();
	let loop: Loop | null = null;

	$effect(() => {
		const current = scene;
		const res = resolution;
		const cap = fps;
		const most = fill === "window" ? WINDOW_DPR_CAP : DPR_CAP;
		if (!host || !canvas) return;
		const box = host;
		const surface = canvas;
		// No 2D context (a test DOM): nothing to paint, so nothing is scheduled.
		const ctx = surface.getContext("2d");
		if (ctx === null) return;
		const read = tokenReader(box);
		let scale = 0;
		let width = -1;
		let height = -1;

		function measure(): void {
			const w = box.clientWidth;
			const h = box.clientHeight;
			const s = canvasScale(window.devicePixelRatio, res, most);
			if (w === width && h === height && s === scale) return;
			width = w;
			height = h;
			scale = s;
			fitCanvas(surface, w, h, s);
			current.resize(w, h);
		}

		const frames = createLoop(windowFrameHost(window), {
			fps: cap,
			paint(clockMs) {
				ctx.setTransform(scale, 0, 0, scale, 0, 0);
				current.draw(ctx, clockMs);
			},
		});
		loop = frames;
		measure();
		current.recolor(read, document);
		frames.paintNow();
		frames.run(untrack(() => animate));

		const observer =
			typeof ResizeObserver === "function"
				? new ResizeObserver(() => {
						measure();
						frames.paintNow();
					})
				: null;
		observer?.observe(box);
		const offTheme = watchTheme(window, () => {
			current.recolor(read, document);
			frames.paintNow();
		});
		const offRatio = watchPixelRatio(window, () => {
			measure();
			frames.paintNow();
		});
		return () => {
			frames.dispose();
			if (loop === frames) loop = null;
			observer?.disconnect();
			offTheme();
			offRatio();
		};
	});

	$effect(() => {
		const on = animate;
		loop?.run(on);
	});
</script>

<div class="ambient" class:ambient--window={fill === "window"} bind:this={host} aria-hidden="true">
	<canvas bind:this={canvas}></canvas>
</div>

<style>
	.ambient {
		position: absolute;
		inset: 0;
		pointer-events: none;
		overflow: hidden;
	}

	/* Behind the whole app, above the root ground — where the orbit halo
	   has always sat. */
	.ambient--window {
		position: fixed;
		z-index: -1;
	}

	canvas {
		width: 100%;
		height: 100%;
		display: block;
	}
</style>
