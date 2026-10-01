<script lang="ts">
	// Start from a template, or save the form as one (TD-3816).
	// Presentational: picking a template fills the draft and does not
	// create a job. Save as template stores this form under a name.
	import { applyTemplate, saveAsTemplate, scheduled, setTemplateName } from '../scheduled.svelte.js';

	function onPick(value: string): void {
		applyTemplate(value);
	}
</script>

<label class="field">
	<span>Start from template</span>
	<select
		aria-label="Start from template"
		value={scheduled.templateId}
		onchange={(e) => onPick(e.currentTarget.value)}
	>
		<option value="">Choose a template</option>
		{#each scheduled.templates as row (row.id)}
			<option value={row.id}>{row.name}</option>
		{/each}
	</select>
	<span class="hint">Fills this form. Nothing is saved until Create.</span>
</label>
<label class="field">
	<span>Template name</span>
	<input
		type="text"
		value={scheduled.templateName}
		oninput={(e) => setTemplateName(e.currentTarget.value)}
	/>
</label>
<button class="action" type="button" onclick={() => saveAsTemplate()}>Save as template</button>
<span class="hint">Saves this form as a template. It does not create a job.</span>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.field input,
	.field select {
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
	}

	.action {
		align-self: flex-start;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		color: var(--color-ink);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-3);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.action:hover {
		background: var(--color-sunken);
	}

	.hint {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}
</style>
