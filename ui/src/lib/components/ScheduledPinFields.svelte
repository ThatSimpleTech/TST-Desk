<script lang="ts">
	// Model pin on the job form (TD-3812). Presentational: the draft store
	// owns the values, and Save / Create decide whether to omit or clear them.
	//
	// A name the catalog no longer lists still has to appear, or an edit of
	// that job would look like "use current" and saving would clear the pin.
	import { scheduled, setDraftField } from '../scheduled.svelte.js';
	import { settings } from '../settings.svelte.js';

	let names = $derived.by(() => {
		const catalog = settings.presets;
		const current = scheduled.draft.preset;
		if (current !== '' && !catalog.includes(current)) return [...catalog, current];
		return catalog;
	});

	function onEngine(value: string): void {
		const engine = value === 'native' || value === 'grok' ? value : '';
		setDraftField('engine', engine);
	}
</script>

<label class="field">
	<span>Model preset</span>
	<select
		value={scheduled.draft.preset}
		onchange={(e) => setDraftField('preset', e.currentTarget.value)}
	>
		<option value="">Use current</option>
		{#each names as name (name)}
			<option value={name}>{name}</option>
		{/each}
	</select>
</label>
<label class="field">
	<span>Engine</span>
	<select value={scheduled.draft.engine} onchange={(e) => onEngine(e.currentTarget.value)}>
		<option value="">Use current</option>
		<option value="native">Native</option>
		<option value="grok">Grok</option>
	</select>
	<span class="hint">Use current follows the window. A chosen preset or engine stays on this job.</span>
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
