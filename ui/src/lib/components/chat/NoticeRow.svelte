<script lang="ts">
	// A line about messages the pane cannot show (TD-5201): an older chat's
	// log no longer reaching its start, older messages left out, or turns a
	// compaction folded into a summary. No one said it, so it is neither a
	// bubble nor an answer: one muted line, and a summary's own text folded
	// under it the way thinking is.
	import type { ChatMessage } from '../../chat-store';
	import { noticeCopy } from '../../chat-history';
	import Disclosure from './Disclosure.svelte';

	let { message }: { message: ChatMessage } = $props();

	let expanded = $state(false);
	const copy = $derived(noticeCopy(message));
</script>

<div class="notice" role="note" aria-label={copy}>
	<p class="line">{copy}</p>
	{#if message.notice === 'summarized' && message.text !== ''}
		<Disclosure {expanded} label="Summary" ontoggle={() => (expanded = !expanded)}>
			{message.text}
		</Disclosure>
	{/if}
</div>

<style>
	.notice {
		width: 100%;
		padding: var(--space-2) 0;
		color: var(--color-ink-muted);
		font-size: var(--text-sm);
	}

	.line {
		margin: 0;
		padding-bottom: var(--space-2);
		border-bottom: var(--border-width) dashed var(--color-hairline);
		text-align: center;
		line-height: var(--leading-normal);
	}

	.notice :global(.fold) {
		margin-top: var(--space-2);
	}
</style>
