<script lang="ts">
	// The one empty state (design consistency round, September 2026).
	//
	// Every pane used to carry its own quiet paragraph, each a little
	// different in size, colour and margin. This is the shared shape: a
	// glyph, a short title, a sentence of body — centred and set in muted
	// ink by default. `align="start"` is for a column whose content reads
	// from the left, so the empty line sits where the first row would.
	// `compact` is the one-liner ("Opening…", "No matches") where a
	// paragraph would weigh more than the pane around it.
	import type { Snippet } from 'svelte';
	import Icon from './Icon.svelte';
	import type { IconName } from '../icons';

	let {
		icon,
		title,
		body,
		compact = false,
		align = 'center',
		children
	}: {
		icon?: IconName;
		title?: string;
		body?: string;
		compact?: boolean;
		align?: 'center' | 'start';
		children?: Snippet;
	} = $props();
</script>

<div class="empty" class:empty--compact={compact} class:empty--start={align === 'start'}>
	{#if icon}
		<span class="glyph" aria-hidden="true"><Icon name={icon} size={compact ? 14 : 20} /></span>
	{/if}
	{#if title}
		<p class="title">{title}</p>
	{/if}
	{#if body}
		<p class="body">{body}</p>
	{/if}
	{#if children}
		<div class="extra">{@render children()}</div>
	{/if}
</div>

<style>
	.empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-1);
		box-sizing: border-box;
		width: 100%;
		max-width: 28rem;
		margin: 0 auto;
		padding: var(--space-6);
		color: var(--color-ink-muted);
		font-size: var(--text-sm);
		line-height: var(--leading-relaxed);
		text-align: center;
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.empty--start {
		align-items: flex-start;
		max-width: none;
		margin: 0;
		padding: var(--space-3) 0 0;
		text-align: left;
	}

	.empty--compact {
		gap: 0;
		padding: var(--space-4) var(--space-2);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		/* A one-liner that can come and go with every keystroke (the
		   palette's "No matches") should not keep re-entering. */
		animation: none;
	}

	.empty--compact.empty--start {
		padding: var(--space-3) 0 0;
	}

	.glyph {
		display: inline-flex;
		margin-bottom: var(--space-1);
		color: var(--color-ink-muted);
		opacity: 0.75;
	}

	.title {
		margin: 0;
		color: var(--color-ink-secondary);
		font-weight: var(--weight-medium);
	}

	.body {
		margin: 0;
	}

	.extra {
		margin-top: var(--space-1);
	}
</style>
