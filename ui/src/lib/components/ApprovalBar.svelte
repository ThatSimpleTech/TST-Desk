<script lang="ts">
	// Approval bar (TD-1007): the shell footer that hosts every pending
	// approval card. Renders nothing while there is nothing to decide, so the
	// panes keep the full height; a card appearing reserves the footer space.
	// JOURNEY2-10: off the chat surface the bar is a named chip that opens
	// the chat, never live Approve and Deny for a chat not on screen.
	import { pending } from '../approval-store.svelte.js';
	import { chatInView, chatLabel } from '../approval-context';
	import { projects, showHome } from '../projects.svelte.js';
	import { sessions } from '../sessions.svelte.js';
	import ApprovalCard from './ApprovalCard.svelte';

	let inView = $derived(chatInView(projects.surface));
	let waitingIn = $derived(
		pending.length > 0 ? chatLabel(pending[0].sessionId, sessions.rows) : ''
	);
</script>

{#if pending.length > 0}
	{#if inView}
		<footer class="approval-bar" aria-label="Pending approvals">
			{#each pending as approval (approval.toolCallId)}
				<ApprovalCard {approval} chat={chatLabel(approval.sessionId, sessions.rows)} />
			{/each}
		</footer>
	{:else}
		<footer class="approval-chip" aria-label="Pending approvals">
			<p class="chip-text" role="status">
				{waitingIn} is waiting for approval{pending.length > 1 ? ` (${pending.length})` : ''}
			</p>
			<button class="chip-review" type="button" onclick={showHome}>Review</button>
		</footer>
	{/if}
{/if}

<style>
	.approval-chip {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		padding: var(--space-2) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		flex-shrink: 0;
	}

	.chip-text {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.chip-review {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.chip-review:hover {
		border-color: var(--color-accent);
	}

	.approval-bar {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		max-height: 40%;
		overflow-y: auto;
		flex-shrink: 0;
	}
</style>
