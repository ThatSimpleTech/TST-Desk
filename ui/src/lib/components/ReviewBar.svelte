<script lang="ts">
	// The Files pane's send bar for diff comments (TD-5132): every draft goes
	// to the agent as one message, or none of them do.
	import { discardDrafts, review, submitDrafts } from '../review-comments.svelte.js';

	let count = $derived(review.drafts.length);
</script>

{#if count > 0 || review.notice}
	<div class="review-bar" role="region" aria-label="Line comments to send">
		<span class="summary" aria-live="polite">
			{#if review.sending}
				Sending {count} {count === 1 ? 'comment' : 'comments'}…
			{:else}
				{count} {count === 1 ? 'comment' : 'comments'} ready
			{/if}
		</span>
		{#if review.notice}<p class="notice" role="alert">{review.notice}</p>{/if}
		<div class="actions">
			<button type="button" class="btn" disabled={review.sending || count === 0} onclick={discardDrafts}
				>Discard comments</button
			>
			<button
				type="button"
				class="btn btn--primary"
				disabled={review.sending || count === 0}
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
