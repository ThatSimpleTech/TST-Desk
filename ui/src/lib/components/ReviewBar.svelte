<script lang="ts">
	// The Files pane's send bar for diff comments (TD-5132): every draft goes
	// to the agent as one message, or none of them do.
	import { discardDrafts, review, submitDrafts } from '../review-comments.svelte.js';

	let count = $derived(review.drafts.length);
	let waiting = $derived(review.waiting.length);
</script>

{#if count > 0 || waiting > 0 || review.notice}
	<div class="review-bar" role="region" aria-label="Line comments to send">
		<span class="summary" aria-live="polite">
			{#if count > 0}{count} {count === 1 ? 'comment' : 'comments'} ready{/if}
			{#if waiting > 0}
				<span class="waiting"
					>{waiting} {waiting === 1 ? 'comment' : 'comments'} sent, waiting for the agent to read
					{waiting === 1 ? 'it' : 'them'}</span
				>
			{/if}
		</span>
		{#if review.notice}<p class="notice" role="alert">{review.notice}</p>{/if}
		<div class="actions">
			<button type="button" class="btn" disabled={count === 0} onclick={discardDrafts}
				>Discard comments</button
			>
			<button
				type="button"
				class="btn btn--primary"
				disabled={count === 0}
				onclick={submitDrafts}>Send comments to agent</button
			>
		</div>
	</div>
{/if}

<style>
	.review-bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2) var(--space-3);
		padding: var(--space-2) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.summary {
		flex: 1;
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-1) var(--space-3);
	}

	.waiting {
		color: var(--color-ink-muted);
	}

	.notice {
		flex-basis: 100%;
		order: 3;
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-err);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
	}

	.btn {
		padding: var(--space-1) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		background: var(--color-ground);
		color: var(--color-ink);
		font-size: var(--text-xs);
		cursor: pointer;
	}

	.btn--primary {
		border-color: var(--color-accent);
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
