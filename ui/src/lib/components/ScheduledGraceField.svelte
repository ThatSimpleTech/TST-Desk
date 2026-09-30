<script lang="ts">
	// How late a slot may be and still run (TD-3813). Presentational: the
	// draft store owns the value. Create omits a blank (always run). Save
	// always sends it, including "" to clear. A grace the select does not
	// list still has to appear, or editing that job would show "Always run"
	// and saving would clear it.
	import { GRACE_CHOICES } from '../scheduled';
	import { scheduled, setDraftField } from '../scheduled.svelte.js';

	let choices = $derived.by(() => {
		const known = GRACE_CHOICES.map(({ value, label }) => ({ value, label }));
		const current = scheduled.draft.grace;
		if (current !== '' && !known.some((choice) => choice.value === current)) {
			return [...known, { value: current, label: current }];
		}
		return known;
	});
</script>

<label class="field">
	<span>If late</span>
	<select value={scheduled.draft.grace} onchange={(e) => setDraftField('grace', e.currentTarget.value)}>
		{#each choices as choice (choice.value)}
			<option value={choice.value}>{choice.label}</option>
		{/each}
	</select>
	<span class="hint">Always run fires a missed slot once. Otherwise a later slot is skipped.</span>
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
