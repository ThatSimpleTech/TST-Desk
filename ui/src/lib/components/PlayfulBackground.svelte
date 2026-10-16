<script lang="ts">
	// Playful background (TD-1750). A field of slow twinkling sparkles under
	// the chat column, and a ring of light that expands from every click.
	// Pure maths lives in playful-bg.ts; this component owns the canvas, DPR
	// scaling, the loop, and token colours — sparkles and ripples both read
	// the accent token, so they land right on both grounds. It sits under
	// the content and never takes pointer events; ripples listen on window
	// so a click anywhere in the pane (through the canvas) lights one off.
	// Reduced motion freezes the field at a static twinkle and skips
	// ripples — same stance as the GlowLayer.
	import {
		pushRipple,
		rippleAlpha,
		rippleAt,
		rippleRadius,
		sparkleField,
		stepRipples,
		staticAlpha,
		shouldAnimate,
		twinkleAlpha,
		type Ripple,
		type Sparkle,
	} from "../playful-bg";

	let canvas: HTMLCanvasElement | undefined = $state();
	let host: HTMLDivElement | undefined = $state();

	let sparkles: Sparkle[] = [];
	let ripples: Ripple[] = [];
	let raf = 0;
	let lastT = 0;
	let reduceQuery: MediaQueryList | null = null;
	let reduced = false;
	let accent = "";

	function tokenColors(): void {
		if (!host) return;
		accent = getComputedStyle(host).getPropertyValue("--color-accent").trim();
	}

	function hexToRgb(hex: string): [number, number, number] | null {
		const m = /^#?([0-9a-f]{6})$/i.exec(hex);
		if (!m) return null;
		const n = parseInt(m[1], 16);
		return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
	}

	function rgba(hex: string, alpha: number): string {
		const rgb = hexToRgb(hex);
		if (!rgb) return "transparent";
		return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha.toFixed(3)})`;
	}

	function rebuildField(): void {
		if (!canvas || !host) return;
		const dpr = Math.min(window.devicePixelRatio || 1, 2);
		const w = host.clientWidth;
		const h = host.clientHeight;
		canvas.width = Math.max(1, Math.round(w * dpr));
		canvas.height = Math.max(1, Math.round(h * dpr));
		const ctx = canvas.getContext("2d");
		if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		sparkles = sparkleField({ seed: 0x5eed, width: w, height: h });
	}

	function draw(tMs: number): void {
		if (!canvas || !host || !accent) return;
		const ctx = canvas.getContext("2d");
		if (!ctx) return;
		const w = host.clientWidth;
		const h = host.clientHeight;
		ctx.clearRect(0, 0, w, h);
		const tSec = tMs / 1000;
		for (const s of sparkles) {
			const a = reduced ? staticAlpha() : twinkleAlpha(s, tMs);
			ctx.fillStyle = rgba(accent, a);
			ctx.beginPath();
			ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
			ctx.fill();
		}
		if (reduced) return; // ripples are motion; static mode skips them.
		for (const r of ripples) {
			const radius = rippleRadius(r, r.ageSec);
			const alpha = rippleAlpha(r, r.ageSec);
			if (radius <= 0 || alpha <= 0) continue;
			ctx.strokeStyle = rgba(accent, alpha);
			ctx.lineWidth = 2;
			ctx.beginPath();
			ctx.arc(r.x, r.y, radius, 0, Math.PI * 2);
			ctx.stroke();
		}
	}

	function tick(tMs: number): void {
		const dt = Math.min((tMs - lastT) / 1000, 0.05);
		lastT = tMs;
		if (ripples.length > 0) ripples = stepRipples(ripples, dt);
		draw(tMs);
		if (shouldAnimate(reduced) || ripples.length > 0) raf = requestAnimationFrame(tick);
	}

	function start(): void {
		if (typeof requestAnimationFrame !== "function") {
			draw(performance.now());
			return;
		}
		cancelAnimationFrame(raf);
		if (shouldAnimate(reduced)) raf = requestAnimationFrame(tick);
		else draw(performance.now());
	}

	function onPointerDown(event: PointerEvent): void {
		if (reduced || !host) return;
		const rect = host.getBoundingClientRect();
		const x = event.clientX - rect.left;
		const y = event.clientY - rect.top;
		if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
		// Seed from the click point, not a counter: same point, same ripple.
		ripples = pushRipple(ripples, rippleAt(x, y, Math.round(x * 7919 + y * 104729)));
		if (typeof requestAnimationFrame === "function") {
			lastT = performance.now();
			start();
		}
	}

	function onReduceChange(): void {
		reduced = reduceQuery?.matches ?? false;
		start();
	}

	$effect(() => {
		if (!canvas || !host) return;
		tokenColors();
		// jsdom (tests) has no matchMedia/ResizeObserver; run static there.
		reduceQuery = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
		reduced = reduceQuery?.matches ?? false;
		reduceQuery?.addEventListener("change", onReduceChange);

		const observer =
			typeof ResizeObserver === "function"
				? new ResizeObserver(() => {
						rebuildField();
						if (!shouldAnimate(reduced)) draw(performance.now());
					})
				: null;
		observer?.observe(host);
		rebuildField();
		start();

		// The canvas never takes pointer events, so clicks are heard on
		// window in the capture phase — a ripple lights under whatever was
		// clicked, buttons and all.
		window.addEventListener("pointerdown", onPointerDown, true);
		return () => {
			cancelAnimationFrame(raf);
			reduceQuery?.removeEventListener("change", onReduceChange);
			window.removeEventListener("pointerdown", onPointerDown, true);
			observer?.disconnect();
		};
	});
</script>

<div class="playful-bg" bind:this={host} aria-hidden="true">
	<canvas bind:this={canvas}></canvas>
</div>

<style>
	.playful-bg {
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
