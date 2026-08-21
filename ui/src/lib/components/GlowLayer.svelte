<script lang="ts">
	// Computer-use glow on the Screen pane (TD-3402). CSS overlay, not a
	// window chrome change. Reduced motion keeps a static border.
	import { cuIndicators } from '../screen-indicator.svelte.js';
	import { reducedMotionIndicators } from '../screen-indicator';
	import { settings } from '../settings.svelte.js';

	let show = $derived(cuIndicators.live && settings.cuGlow);
	let staticGlow = $derived(reducedMotionIndicators(cuIndicators.prefersReducedMotion).staticGlow);
</script>

{#if show}
	<div
		class="glow"
		class:glow--static={staticGlow}
		aria-hidden="true"
	></div>
{/if}

<style>
	.glow {
		position: absolute;
		inset: 0;
		pointer-events: none;
		border: calc(var(--border-width) * 2) solid var(--color-accent);
		box-shadow: 0 0 var(--space-4) color-mix(in srgb, var(--color-accent) 40%, transparent);
		animation: glow-pulse 1.6s ease-in-out infinite alternate;
	}

	.glow--static {
		animation: none;
		box-shadow: none;
	}

	@keyframes glow-pulse {
		from {
			opacity: 0.7;
		}
		to {
			opacity: 1;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.glow {
			animation: none;
			box-shadow: none;
		}
	}
</style>
