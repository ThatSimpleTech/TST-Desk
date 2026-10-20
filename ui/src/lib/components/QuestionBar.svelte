<script lang="ts">
	// Question bar (TD-5121): the shell footer that hosts the agent's
	// `ask_user` questions. Renders nothing while there is nothing to answer.
	import { pending } from '../question-store.svelte.js';
	import QuestionCard from './QuestionCard.svelte';
</script>

{#if pending.length > 0}
	<footer class="question-bar" aria-label="Questions from the agent">
		{#each pending as card (card.toolCallId)}
			<QuestionCard {card} />
		{/each}
	</footer>
{/if}

<style>
	.question-bar {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		max-height: 60%;
		overflow-y: auto;
		flex-shrink: 0;
	}
</style>
