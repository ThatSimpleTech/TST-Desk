<script lang="ts">
	// Plan review bar (TD-5123): the shell footer that hosts the plan an
	// `exit_plan_mode` call put up. Renders nothing while no plan waits.
	import { pending } from '../plan-review-store.svelte.js';
	import PlanReviewCard from './PlanReviewCard.svelte';
</script>

{#if pending.length > 0}
	<footer class="plan-bar" aria-label="Plan from the agent">
		{#each pending as card (card.toolCallId)}
			<PlanReviewCard {card} />
		{/each}
	</footer>
{/if}

<style>
	.plan-bar {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		max-height: 70%;
		overflow-y: auto;
		flex-shrink: 0;
	}
</style>
