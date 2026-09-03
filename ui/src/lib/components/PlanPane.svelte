<script lang="ts">
	import EmptyState from './EmptyState.svelte';
	import { approveGrokPlan, grok } from '../grok.svelte.js';
	import { session } from '../session-status.svelte.js';

	let comment = $state('');

	function approve(): void {
		if (session.sessionId === null) return;
		approveGrokPlan(session.sessionId, comment);
		comment = '';
	}

	let hasPlan = $derived(grok.planMarkdown.length > 0 || grok.planEntries.length > 0);
</script>

<div class="plan">
	{#if !hasPlan}
		<EmptyState
			align="start"
			body="When Grok is in plan mode, the plan lands here. Approve it to start implementing, or type a revision note first."
		/>
	{:else}
		{#if grok.planEntries.length > 0}
			<ol class="entries">
				{#each grok.planEntries as entry, i (i)}
					<li>
						<span class="status">{entry.status ?? 'pending'}</span>
						{entry.content}
					</li>
				{/each}
			</ol>
		{/if}
		{#if grok.planMarkdown}
			<pre class="md">{grok.planMarkdown}</pre>
		{/if}
		<div class="actions">
			<input
				class="note"
				type="text"
				placeholder="Optional revision note"
				bind:value={comment}
			/>
			<button type="button" class="go" onclick={approve}>Approve plan</button>
		</div>
	{/if}
</div>

<style>
	.plan {
		height: 100%;
		overflow-y: auto;
		padding: var(--space-3);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}
	.entries {
		margin: 0;
		padding-left: 1.2rem;
		font-size: var(--text-sm);
	}
	.status {
		color: var(--color-ink-muted);
		margin-right: 0.4rem;
		font-size: 0.75rem;
		text-transform: uppercase;
	}
	.md {
		white-space: pre-wrap;
		font-size: var(--text-sm);
		margin: 0;
		font-family: var(--font-mono);
	}
	.actions {
		display: flex;
		gap: var(--space-2);
	}
	.note {
		flex: 1;
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		color: inherit;
		padding: 6px 8px;
		border-radius: var(--radius-md);
	}
	.go {
		background: var(--color-accent);
		color: var(--color-on-accent);
		border: 0;
		border-radius: var(--radius-md);
		padding: 6px 10px;
		cursor: pointer;
	}
</style>
