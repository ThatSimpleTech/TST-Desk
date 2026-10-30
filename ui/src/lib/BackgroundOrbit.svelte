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
	import type { BackgroundProps } from './backgrounds/scene';

	let { animate, fill = 'window' }: BackgroundProps = $props();

	/** At tile size the real dots would be specks under a pixel. */
	const dotScale = $derived(fill === 'parent' ? 3.2 : 1);

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

<div
	class="orbit"
	class:orbit--parent={fill === 'parent'}
	class:orbit--still={!animate}
	role="presentation"
	aria-hidden="true"
>
	<div class="wash"></div>
	<div class="field">
		<svg viewBox="0 0 1200 1040" preserveAspectRatio="xMidYMid slice">
			<g class="glow">
				{#each dots as d}
					<circle cx={d.x} cy={d.y} r={d.r * dotScale} fill="var(--bg-orbit-dot)" opacity={d.a} />
				{/each}
			</g>
			<g class="anchor">
				<circle cx={CX} cy={CY} r={2.6 * dotScale} fill="var(--bg-orbit-dot-hot)" />
			</g>
		</svg>
	</div>
	<div class="vignette"></div>
</div>

<style>
	.orbit {
		position: fixed;
		inset: 0;
		z-index: -1;
		pointer-events: none;
		opacity: var(--bg-orbit-opacity);
		overflow: hidden;
	}

	.orbit--parent {
		position: absolute;
		z-index: auto;
	}

	.wash,
	.vignette {
		position: absolute;
		inset: 0;
	}

	/* Faint cast of light behind the orbit core. */
	.wash {
		background: radial-gradient(
			42vmin 42vmin at calc(50% - 2%) calc(50% - 3%),
			var(--bg-orbit-wash),
			transparent 70%
		);
		animation: breathe 14s ease-in-out infinite;
	}

	.field {
		position: absolute;
		inset: -8%;
		animation: drift 80s linear infinite;
	}

	svg {
		width: 100%;
		height: 100%;
		display: block;
	}

	.glow {
		filter: drop-shadow(0 0 6px var(--bg-orbit-glow));
		animation: pulse 9s ease-in-out infinite;
	}

	.anchor {
		filter: drop-shadow(0 0 9px var(--bg-orbit-glow));
		animation: pulse 6s ease-in-out infinite;
	}

	/* The field drifts and breathes; the anchor stays still. Slow and calm —
	   no bounce, no snap. */
	@keyframes drift {
		from {
			transform: rotate(0deg) scale(1);
		}
		50% {
			transform: rotate(180deg) scale(1.02);
		}
		to {
			transform: rotate(360deg) scale(1);
		}
	}

	@keyframes breathe {
		0%,
		100% {
			opacity: 0.75;
		}
		50% {
			opacity: 1;
		}
	}

	@keyframes pulse {
		0%,
		100% {
			opacity: 0.7;
		}
		50% {
			opacity: 1;
		}
	}

	.orbit--still .field,
	.orbit--still .wash,
	.orbit--still .glow,
	.orbit--still .anchor {
		animation: none;
	}

	@media (prefers-reduced-motion: reduce) {
		.field,
		.wash,
		.glow,
		.anchor {
			animation: none;
		}
	}
</style>
