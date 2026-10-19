<script lang="ts">
	// Steering proposal bar (TD-5140): the shell footer that hosts the
	// agent's proposed steering edits. Renders nothing while there is
	// nothing to decide.
	import { pending } from '../steering-proposal-store.svelte.js';
	import SteeringProposalCard from './SteeringProposalCard.svelte';
</script>

{#if pending.length > 0}
	<footer class="steering-bar" aria-label="Steering edit proposals">
		{#each pending as proposal (proposal.proposalId)}
			<SteeringProposalCard {proposal} />
		{/each}
	</footer>
{/if}

<style>
	.steering-bar {
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
