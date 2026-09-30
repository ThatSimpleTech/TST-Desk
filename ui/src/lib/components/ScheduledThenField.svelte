<script lang="ts">
	// The job to start once after this one ends ok (TD-3817).
	// Presentational: the draft store owns the id. Create omits None.
	// Save always sends the id, including "" to clear. The value is the
	// other job's id; the label is its instruction. The job being edited
	// is left out, because a job cannot follow itself. An id the list no
	// longer has still has to appear, or Save would clear a link the user
	// did not touch.
	import { scheduled, setDraftField } from '../scheduled.svelte.js';

	let choices = $derived.by(() => {
		const options = scheduled.items
			.filter((row) => row.id !== scheduled.editingId)
			.map((row) => ({ value: row.id, label: row.instruction }));
		const current = scheduled.draft.then;
		if (current !== '' && !options.some((option) => option.value === current)) {
			options.push({ value: current, label: current });
		}
		return options;
	});
</script>

<label class="field">
	<span>Then run</span>
	<select
		value={scheduled.draft.then}
		onchange={(e) => setDraftField('then', e.currentTarget.value)}
	>
		<option value="">None</option>
		{#each choices as choice (choice.value)}
			<option value={choice.value}>{choice.label}</option>
		{/each}
	</select>
	<span class="hint">Runs once after this job succeeds. The other job keeps its own schedule.</span>
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
