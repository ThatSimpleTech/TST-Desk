<script lang="ts">
	// Software agent cursor on the Screen pane (TD-3402).
	// Not a second hardware pointer — pointer-events none, no OS cursor API.
	import { cuIndicators } from '../screen-indicator.svelte.js';
	import { cursorPercent, reducedMotionIndicators } from '../screen-indicator';
	import { settings } from '../settings.svelte.js';

	let show = $derived(cuIndicators.live && settings.cuAgentCursor);
	let trail = $derived(reducedMotionIndicators(cuIndicators.prefersReducedMotion).trail);
	let pos = $derived(
		cursorPercent(
			cuIndicators.cursorX,
			cuIndicators.cursorY,
			cuIndicators.frameWidth,
			cuIndicators.frameHeight,
		),
	);
</script>

{#if show}
	<div
		class="cursor"
		class:cursor--trail={trail}
		style="left: {pos.left}%; top: {pos.top}%"
		aria-hidden="true"
	></div>
{/if}

<style>
	.cursor {
		position: absolute;
		width: var(--space-4);
		height: var(--space-4);
		margin-left: calc(var(--space-4) / -2);
		margin-top: calc(var(--space-4) / -2);
		border-radius: var(--radius-full);
		background: var(--color-accent);
		border: var(--border-width) solid var(--color-on-accent);
		pointer-events: none;
	}

	.cursor--trail {
		transition:
			left var(--transition-base),
			top var(--transition-base);
	}

	.cursor--trail::after {
		content: '';
		position: absolute;
		inset: calc(var(--space-1) * -1);
		border-radius: var(--radius-full);
		border: var(--border-width) solid var(--color-accent);
		opacity: 0.45;
	}

	@media (prefers-reduced-motion: reduce) {
		.cursor--trail {
			transition: none;
		}
		.cursor--trail::after {
			content: none;
		}
	}
</style>
