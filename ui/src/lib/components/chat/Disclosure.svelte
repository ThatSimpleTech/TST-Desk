<script lang="ts">
	// Shared fold chrome for reasoning and tool blocks (TD-1902).
	// Presentational: the caller owns expanded/live/label and the body.
	import type { Snippet } from 'svelte';
	import Icon from '../Icon.svelte';

	let {
		expanded,
		live = false,
		label,
		ontoggle,
		children,
	}: {
		expanded: boolean;
		live?: boolean;
		label: string;
		ontoggle: () => void;
		children: Snippet;
	} = $props();
</script>

<div class="fold" class:live>
	<button class="summary" type="button" aria-expanded={expanded} onclick={ontoggle}>
		<span class="chevron" class:open={expanded} aria-hidden="true">
			<Icon name="chevron-down" size={11} />
		</span>
		<span class="label" class:shimmer={live}>{label}</span>
	</button>
	{#if expanded}
		<div class="body">{@render children()}</div>
	{/if}
</div>

<style>
	.fold {
		margin-bottom: var(--space-3);
	}

	.summary {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) 0;
		border: none;
		background: transparent;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		cursor: pointer;
	}

	.summary:hover .label {
		color: var(--color-ink);
	}

	.chevron {
		display: inline-flex;
		transform: rotate(-90deg);
		transition: transform var(--transition-fast);
	}

	.chevron.open {
		transform: rotate(0deg);
	}

	.body {
		margin-top: var(--space-2);
		padding-left: var(--space-3);
		border-left: var(--border-width) solid var(--color-hairline);
		font-size: var(--text-sm);
		line-height: var(--leading-relaxed);
		color: var(--color-ink-muted);
		white-space: pre-wrap;
		user-select: text;
	}

	.shimmer {
		background: linear-gradient(
			100deg,
			var(--color-ink-muted) 40%,
			var(--color-ink) 50%,
			var(--color-ink-muted) 60%
		);
		background-size: 200% 100%;
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
		animation: fold-shimmer 1.6s linear infinite;
	}

	@keyframes fold-shimmer {
		from {
			background-position: 100% 0;
		}
		to {
			background-position: -100% 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.chevron {
			transition: none;
		}

		.shimmer {
			animation: none;
			background: none;
			color: var(--color-ink-muted);
		}
	}
</style>
