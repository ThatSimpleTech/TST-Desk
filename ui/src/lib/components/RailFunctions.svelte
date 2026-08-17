<script lang="ts">
	// The rail's function-surface group (TD-1712): Home, Projects, Scheduled,
	// grouped above session history. The registry in ../rail.ts decides each
	// row's state, and only a `ready` one is clickable — `current` renders
	// selected (you are already there) and `planned` renders muted with its
	// milestone. The store's dispatcher refuses both, so neither the markup
	// nor a future caller can produce a click that goes nowhere.
	import Icon from './Icon.svelte';
	import { RAIL_FUNCTIONS, entryHint } from '../rail';
	import { activateRailFunction } from '../sessions.svelte.js';

	// `compact` is the rail's collapsed 48px strip: icon only, no labels.
	let { compact = false }: { compact?: boolean } = $props();
</script>

<nav class="fns" class:fns-compact={compact} aria-label="Surfaces">
	{#each RAIL_FUNCTIONS as entry (entry.id)}
		<button
			class="fn"
			class:fn-compact={compact}
			class:fn-current={entry.state === 'current'}
			type="button"
			disabled={entry.state !== 'ready'}
			aria-current={entry.state === 'current' ? 'page' : undefined}
			title={entryHint(entry)}
			aria-label={entryHint(entry)}
			onclick={() => activateRailFunction(entry.id)}
		>
			<Icon name={entry.icon} size={16} />
			{#if !compact}
				<span class="fn-label">{entry.label}</span>
				{#if entry.note !== null}
					<span class="fn-note">{entry.note}</span>
				{/if}
			{/if}
		</button>
	{/each}
</nav>

<style>
	.fns {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		padding: 0 var(--space-2) var(--space-2);
		flex-shrink: 0;
	}

	.fns-compact {
		align-items: center;
		width: 100%;
		padding: 0 0 var(--space-1);
		border-bottom: var(--border-width) solid var(--color-hairline);
	}

	.fn {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		text-align: left;
		border: none;
		background: transparent;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		color: var(--color-ink);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.fn-compact {
		width: 28px;
		height: 28px;
		justify-content: center;
		padding: var(--space-0);
	}

	.fn:hover:not(:disabled) {
		background: var(--color-lifted);
	}

	/* A surface that isn't ready reads as inert, not broken: no hover, no
	   pointer. */
	.fn:disabled {
		color: var(--color-ink-muted);
		cursor: default;
	}

	/* …except the current surface, which reads as selected — the session rows
	   below mark the attached session the same way. */
	.fn-current:disabled {
		color: var(--color-ink);
		background: var(--color-lifted);
		box-shadow: inset 2px 0 0 var(--color-accent);
	}

	.fn-label {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.fn-note {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: 0 var(--space-2);
		line-height: var(--leading-relaxed);
	}
</style>
