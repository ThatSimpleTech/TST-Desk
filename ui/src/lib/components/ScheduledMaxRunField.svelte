<script lang="ts">
	// How long this job's turn may run (TD-3819). Presentational: the
	// draft store owns the value. Create omits a blank (the configured
	// limit). Save always sends it, including "" to clear. A limit the
	// select does not list still has to appear, or editing that job would
	// show Default and saving would clear it.
	import { MAX_RUN_CHOICES } from '../scheduled-max-run';
	import { scheduled, setDraftField } from '../scheduled.svelte.js';

	let choices = $derived.by(() => {
		const known = MAX_RUN_CHOICES.map(({ value, label }) => ({ value, label }));
		const current = scheduled.draft.max_run;
		if (current !== '' && !known.some((choice) => choice.value === current)) {
			return [...known, { value: current, label: current }];
		}
		return known;
	});
</script>

<label class="field">
	<span>Max run time</span>
	<select
		value={scheduled.draft.max_run}
		onchange={(e) => setDraftField('max_run', e.currentTarget.value)}
	>
		{#each choices as choice (choice.value)}
			<option value={choice.value}>{choice.label}</option>
		{/each}
	</select>
	<span class="hint">Default uses the configured limit. A choice stops the turn and does not retry it.</span>
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
