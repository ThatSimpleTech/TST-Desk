<script lang="ts">
	// Ambient "Orbit Halo" background (the photo reference): concentric rings
	// of glowing dots around a still focal point, breathing slowly over the
	// page ground. Purely presentational — fixed, non-interactive, painted
	// under everything (z-index: -1, above the root background).
	//
	// All colour comes from the --bg-orbit-* tokens in tokens.css, so light
	// and dark themes swap the mood automatically: faint warm in light, the
	// cyan glow-on-near-black in dark. Geometry is theme-independent.
	//
	// Since TD-5202 it is Classic's window layer: `animate` false holds it
	// still, and `fill="parent"` shrinks it into a Settings preview tile,
	// where the dots are drawn bigger so the rings still read at that size.
	// It moves on Classic's shared tick (backgrounds/classic.ts), which
	// writes the pose as inline styles once a tick; with motion off the
	// styles go and the stylesheet's resting pose shows.
	import { CLASSIC_FPS, classicTick, orbitPose, type OrbitPose } from './backgrounds/classic';
	import { createLoop, windowFrameHost } from './backgrounds/loop';
	import { inPreview } from './backgrounds/preview';
	import type { BackgroundProps } from './backgrounds/scene';

	let { animate, fill = 'window' }: BackgroundProps = $props();

	/** At tile size the real dots would be specks under a pixel. */
	const dotScale = $derived(fill === 'parent' ? 3.2 : 1);

	/** A preview tile runs a loop of its own that keeps going out of focus:
	 *  it moves exactly while the pointer or keyboard focus is on it. */
	const preview = inPreview();

	let pose: OrbitPose | null = $state.raw(null);

	$effect(() => {
		if (!animate) {
			pose = null;
			return;
		}
		const paint = (clockMs: number): void => {
			pose = orbitPose(clockMs);
		};
		const frames = preview
			? createLoop(windowFrameHost(window, { restUnfocused: false }), { fps: CLASSIC_FPS, paint })
			: classicTick.join(paint);
		frames.paintNow();
		frames.run(true);
		return () => frames.dispose();
	});

	const CX = 560;
	const CY = 520;

	/** Dot radii grow outward; opacity falls outward, so the field reads
	 * dense and bright at the core and dissolves at the edges. */
	const RINGS = [
		{ r: 70, step: 12, dot: 1.1, alpha: 0.85 },
		{ r: 120, step: 14, dot: 1.3, alpha: 0.7 },
		{ r: 175, step: 16, dot: 1.5, alpha: 0.58 },
		{ r: 235, step: 18, dot: 1.8, alpha: 0.46 },
		{ r: 300, step: 20, dot: 2.1, alpha: 0.36 },
		{ r: 370, step: 22, dot: 2.4, alpha: 0.27 },
		{ r: 445, step: 24, dot: 2.7, alpha: 0.19 },
		{ r: 525, step: 26, dot: 3.1, alpha: 0.12 },
	] as const;

	type Dot = { x: number; y: number; r: number; a: number };

	const dots: Dot[] = RINGS.flatMap(({ r, step, dot, alpha }) =>
		Array.from({ length: Math.round(360 / step) }, (_, i) => {
			const angle = ((i * step + r) * Math.PI) / 180; // per-ring offset: rings don't line up into spokes
			return {
				x: CX + r * Math.cos(angle),
				y: CY + r * Math.sin(angle),
				r: dot,
				a: alpha,
			};
		}),
	);
</script>

<div class="orbit" class:orbit--parent={fill === 'parent'} role="presentation" aria-hidden="true">
	<div class="wash" style:opacity={pose?.wash}></div>
	<div class="field" style:transform={pose && `rotate(${pose.turnDeg}deg) scale(${pose.scale})`}>
		<svg viewBox="0 0 1200 1040" preserveAspectRatio="xMidYMid slice">
			<g class="glow" style:opacity={pose?.glow}>
				{#each dots as d}
					<circle cx={d.x} cy={d.y} r={d.r * dotScale} fill="var(--bg-orbit-dot)" opacity={d.a} />
				{/each}
			</g>
			<g class="anchor" style:opacity={pose?.anchor}>
				<circle cx={CX} cy={CY} r={2.6 * dotScale} fill="var(--bg-orbit-dot-hot)" />
			</g>
		</svg>
	</div>
	<div class="vignette"></div>
</div>

<style>
	/* No group opacity here: light's fainter orbit is folded into the
	   --bg-orbit-* alphas, because an opacity on this full-window box costs
	   a full-window transparency layer on every repaint in software. */
	.orbit {
		position: fixed;
		inset: 0;
		z-index: -1;
		pointer-events: none;
		overflow: hidden;
	}

	.orbit--parent {
		position: absolute;
		z-index: auto;
	}

	.vignette {
		position: absolute;
		inset: 0;
	}

	/* Faint cast of light behind the orbit core: the gradient it always was
	   (42vmin, gone by 70%, centred at 48% 47%), in a box just big enough to
	   hold it, so a repaint fills that square instead of the whole window. */
	.wash {
		position: absolute;
		left: calc(48% - 29.4vmin);
		top: calc(47% - 29.4vmin);
		width: 58.8vmin;
		height: 58.8vmin;
		background: radial-gradient(circle closest-side, var(--bg-orbit-wash), transparent);
	}

	.field {
		position: absolute;
		inset: -8%;
	}

	svg {
		width: 100%;
		height: 100%;
		display: block;
	}

	.glow {
		filter: drop-shadow(0 0 6px var(--bg-orbit-glow));
	}

	.anchor {
		filter: drop-shadow(0 0 9px var(--bg-orbit-glow));
	}
</style>
