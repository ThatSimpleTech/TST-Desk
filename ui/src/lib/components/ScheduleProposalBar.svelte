<script lang="ts">
	// Schedule proposal bar (TD-5263): hosts the agent's proposed jobs.
	// Renders nothing while there is nothing to decide.
	import { pending } from '../schedule-proposal-store.svelte.js';
	import ScheduleProposalCard from './ScheduleProposalCard.svelte';
</script>

{#if pending.length > 0}
	<footer class="schedule-bar" aria-label="Scheduled job proposals">
		{#each pending as proposal (proposal.proposalId)}
			<ScheduleProposalCard {proposal} />
		{/each}
	</footer>
{/if}

<style>
	.schedule-bar {
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
