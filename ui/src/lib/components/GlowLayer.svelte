<script lang="ts">
	// Computer-use glow on the Screen frame (TD-3402). CSS overlay on the
	// preview, not the pane and not a window-chrome change. Design mode
	// owns that pointer path, so the glow yields while it is on.
	import { cuIndicators } from '../screen-indicator.svelte.js';
	import { reducedMotionIndicators } from '../screen-indicator';
	import { settings } from '../settings.svelte.js';
	import { design } from '../design.svelte.js';

	let show = $derived(cuIndicators.live && settings.cuGlow && !design.enabled);
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
		box-shadow: 0 0 var(--space-3) color-mix(in srgb, var(--color-accent) 22%, transparent);
		animation: glow-pulse 1.6s ease-in-out infinite alternate;
	}

	.glow--static {
		animation: none;
		box-shadow: none;
	}

	@keyframes glow-pulse {
		from {
			opacity: 0.65;
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
