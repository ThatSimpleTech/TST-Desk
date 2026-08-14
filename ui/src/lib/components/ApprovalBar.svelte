<script lang="ts">
	// Approval bar (TD-1007): the shell footer that hosts every pending
	// approval card. Renders nothing while there is nothing to decide, so the
	// panes keep the full height; a card appearing reserves the footer space.
	import { pending } from '../approval-store';
	import ApprovalCard from './ApprovalCard.svelte';
</script>

{#if pending.length > 0}
	<footer class="approval-bar" aria-label="Pending approvals">
		{#each pending as approval (approval.toolCallId)}
			<ApprovalCard {approval} />
		{/each}
	</footer>
{/if}

<style>
	.approval-bar {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-border);
		background: var(--color-bg-raised);
		max-height: 40%;
		overflow-y: auto;
		flex-shrink: 0;
	}
</style>
