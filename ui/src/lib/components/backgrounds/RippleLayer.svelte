<script lang="ts">
	// Click ripples (TD-1750): every click in the chat pane sends a ring of
	// light out from the pointer. Since TD-5202 they are their own layer,
	// shared by every background but Off, and drawn exactly as before (the
	// accent ring and its maths in playful-bg.ts).
	//
	// Frames run only while a ring is alive, at the display's own rate — a
	// ring crosses 260 px in under a second, so the 30 fps ambient cap would
	// show its steps — and stop when the last one fades. Idle, this layer
	// schedules nothing. The canvas never takes pointer events: clicks are
	// heard on window in the capture phase, so a ring lights under whatever
	// was clicked, buttons and all.
	import { pushRipple, rippleAlpha, rippleAt, rippleRadius, stepRipples, type Ripple } from "../../playful-bg";
	import { createLoop, windowFrameHost } from "../../backgrounds/loop";
	import { canvasScale, cssColor, fitCanvas, tokenColor, tokenReader } from "../../backgrounds/paint";

	let host: HTMLDivElement | undefined = $state();
	let canvas: HTMLCanvasElement | undefined = $state();

	$effect(() => {
		if (!host || !canvas) return;
		const box = host;
		const surface = canvas;
		const ctx = surface.getContext("2d");
		if (ctx === null) return;
		const read = tokenReader(box);
		let ripples: Ripple[] = [];
		let accent = "";
		let scale = 1;
		let width = 0;
		let height = 0;
		let lastClock = 0;

		function measure(): void {
			width = box.clientWidth;
			height = box.clientHeight;
			scale = canvasScale(window.devicePixelRatio);
			fitCanvas(surface, width, height, scale);
		}

		const frames = createLoop(windowFrameHost(window), {
			fps: null,
			paint(clockMs) {
				ripples = stepRipples(ripples, Math.min((clockMs - lastClock) / 1000, 0.05));
				lastClock = clockMs;
				ctx.setTransform(scale, 0, 0, scale, 0, 0);
				ctx.clearRect(0, 0, width, height);
				ctx.strokeStyle = accent;
				ctx.lineWidth = 2;
				for (const r of ripples) {
					const radius = rippleRadius(r, r.ageSec);
					const alpha = rippleAlpha(r, r.ageSec);
					if (radius <= 0 || alpha <= 0) continue;
					ctx.globalAlpha = alpha;
					ctx.beginPath();
					ctx.arc(r.x, r.y, radius, 0, Math.PI * 2);
					ctx.stroke();
				}
				ctx.globalAlpha = 1;
				if (ripples.length === 0) frames.run(false);
			},
		});

		function onPointerDown(event: PointerEvent): void {
			const rect = box.getBoundingClientRect();
			const x = event.clientX - rect.left;
			const y = event.clientY - rect.top;
			if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
			// Read on the click, so a theme switch since the last one shows.
			accent = cssColor(tokenColor(read, "--color-accent"));
			measure();
			// Seed from the click point, not a counter: same point, same ripple.
			ripples = pushRipple(ripples, rippleAt(x, y, Math.round(x * 7919 + y * 104729)));
			frames.run(true);
		}

		window.addEventListener("pointerdown", onPointerDown, true);
		return () => {
			frames.dispose();
			window.removeEventListener("pointerdown", onPointerDown, true);
		};
	});
</script>

<div class="ripples" bind:this={host} aria-hidden="true">
	<canvas bind:this={canvas}></canvas>
</div>

<style>
	.ripples {
		position: absolute;
		inset: 0;
		pointer-events: none;
		overflow: hidden;
	}

	canvas {
		width: 100%;
		height: 100%;
		display: block;
	}
</style>
