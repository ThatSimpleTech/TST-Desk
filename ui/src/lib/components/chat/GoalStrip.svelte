<script lang="ts">
	// The session's goal above the composer (TD-5124). One line, display
	// only: `/goal` changes it, and a passing done-check clears it. The full
	// text is in the tooltip and for screen readers.
	let { goal }: { goal: string | null } = $props();
</script>

{#if goal !== null}
	<section class="strip" aria-label="Session goal" title={goal}>
		<span class="tag">Goal</span>
		<span class="text">{goal}</span>
	</section>
{/if}

<style>
	.strip {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		margin: 0 var(--space-4) var(--space-2);
		padding: var(--space-1) var(--space-3);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-lg);
		min-width: 0;
	}

	.tag {
		flex: none;
		font-weight: var(--weight-semibold);
		color: var(--color-accent);
	}

	.text {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
