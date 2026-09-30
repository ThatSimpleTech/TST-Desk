<script lang="ts">
	// Extra tries after a transient scheduled failure (TD-3814).
	// Presentational: the draft store owns the count. Create omits None.
	// Save always sends the count, including 0 to clear. The delay is 10
	// minutes; it is not a second control. A count the select does not
	// list still has to appear, or editing that job would show None and
	// saving would clear it.
	import { RETRY_CHOICES } from '../scheduled';
	import { scheduled, setDraftField } from '../scheduled.svelte.js';

	let choices = $derived.by(() => {
		const known = RETRY_CHOICES.map(({ value, label }) => ({ value, label }));
		const current = scheduled.draft.retries;
		if (current !== '' && !known.some((choice) => choice.value === current)) {
			return [...known, { value: current, label: current }];
		}
		return known;
	});
</script>

<label class="field">
	<span>Retries</span>
	<select
		value={scheduled.draft.retries}
		onchange={(e) => setDraftField('retries', e.currentTarget.value)}
	>
		{#each choices as choice (choice.value)}
			<option value={choice.value}>{choice.label}</option>
		{/each}
	</select>
	<span class="hint">None runs once. Otherwise a transient failure tries again after 10 min.</span>
</label>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.field select {
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
	}

	.hint {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}
</style>
