<script lang="ts">
	// Memory proposal bar (TD-2402): the shell footer that hosts the
	// distill card. Renders nothing while there is nothing to decide.
	import { pending } from '../memory-proposal-store.svelte.js';
	import MemoryProposalCard from './MemoryProposalCard.svelte';
</script>

{#if pending.length > 0}
	<footer class="memory-bar" aria-label="Memory proposal">
		{#each pending as proposal (proposal.proposalId)}
			<MemoryProposalCard {proposal} />
		{/each}
	</footer>
{/if}

<style>
	.memory-bar {
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
