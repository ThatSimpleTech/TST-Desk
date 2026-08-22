<script lang="ts">
	// Software agent cursor on the Screen frame (TD-3402).
	// Not a second hardware pointer — pointer-events none, no OS cursor API.
	// Hidden until a move/click has a point, and while Design mode is on.
	import { cuIndicators } from '../screen-indicator.svelte.js';
	import { cursorPercent, cursorVisible, reducedMotionIndicators } from '../screen-indicator';
	import { settings } from '../settings.svelte.js';
	import { design } from '../design.svelte.js';
	import Icon from './Icon.svelte';

	let placed = $derived(
		cursorVisible(
			cuIndicators.cursorX,
			cuIndicators.cursorY,
			cuIndicators.frameWidth,
			cuIndicators.frameHeight,
		),
	);
	let show = $derived(
		cuIndicators.live && settings.cuAgentCursor && placed && !design.enabled,
	);
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
	>
		<Icon name="mouse-pointer" size={16} />
	</div>
{/if}

<style>
	.cursor {
		position: absolute;
		width: 16px;
		height: 16px;
		/* Tip of the pointer sits on the last move/click, not the glyph's center. */
		margin-left: -2px;
		margin-top: -2px;
		color: var(--color-accent);
		pointer-events: none;
	}

	.cursor--trail {
		transition:
			left var(--transition-base),
			top var(--transition-base);
	}

	@media (prefers-reduced-motion: reduce) {
		.cursor--trail {
			transition: none;
		}
	}
</style>
