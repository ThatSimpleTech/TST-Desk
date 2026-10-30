<script lang="ts">
	// The form for a new list item or group (TD-5203): the fields its model
	// asks for, typed as text. A command or pattern list is typed on one line
	// and split the way a shell splits words; a free-form value such as a
	// model's extra_body is typed as JSON. Nothing is sent until every field
	// reads; the daemon still judges the whole file before writing.
	import type { ConfigField, ConfigModelDef, JsonValue } from '../../protocol';
	import { configEditor } from '../../config-editor.svelte.js';
	import {
		choicesFor,
		newEntry,
		parseJsonText,
		parseNumber,
		parseText,
		splitWords,
	} from '../../config-editor.js';

	let {
		def,
		idPrefix,
		label,
		onsubmit,
		oncancel,
	}: {
		def: ConfigModelDef;
		idPrefix: string;
		label: string;
		onsubmit: (value: Record<string, JsonValue>) => void;
		oncancel: () => void;
	} = $props();

	const fields = $derived(
		def.add_fields
			.map((name) => def.fields.find((f) => f.name === name))
			.filter((f): f is ConfigField => f !== undefined),
	);
	let drafts = $state<Record<string, string>>({});
	let checks = $state<Record<string, boolean>>({});
	let error = $state('');

	function read(field: ConfigField): { ok: true; value: JsonValue } | { ok: false; error: string } {
		const node = field.node;
		const text = (drafts[field.name] ?? '').trim();
		if (node.kind === 'bool') return { ok: true, value: checks[field.name] === true };
		if (text === '') {
			if (field.required) return { ok: false, error: `${field.name} is needed.` };
			return { ok: true, value: null };
		}
		if (node.kind === 'list') return { ok: true, value: splitWords(text) };
		if (node.kind === 'int' || node.kind === 'float') {
			const parsed = parseNumber(text, node);
			return parsed.ok ? parsed : { ok: false, error: `${field.name}: ${parsed.error}` };
		}
		if (node.kind === 'json') {
			const parsed = parseJsonText(text, node);
			return parsed.ok ? parsed : { ok: false, error: `${field.name}: ${parsed.error}` };
		}
		const parsed = parseText(text, node);
		return parsed.ok ? parsed : { ok: false, error: `${field.name}: ${parsed.error}` };
	}

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		const values: Record<string, JsonValue> = {};
		for (const field of fields) {
			const result = read(field);
			if (!result.ok) {
				error = result.error;
				return;
			}
			values[field.name] = result.value;
		}
		error = '';
		onsubmit(newEntry(def, values));
	}
</script>

<form class="cfg-form" aria-label={label} onsubmit={submit}>
	{#each fields as field (field.name)}
		{@const fid = `${idPrefix}-${field.name}`}
		<div class="cfg-form-row">
			<label class="cfg-label" for={fid}>
				<span>{field.name}</span>
				{#if field.node.unit}<span class="cfg-unit">{field.node.unit}</span>{/if}
				{#if field.required}<span class="cfg-unit">needed</span>{/if}
			</label>
			{#if field.node.kind === 'bool'}
				<input id={fid} type="checkbox" bind:checked={checks[field.name]} />
			{:else if field.node.kind === 'enum' || field.node.choices_from.length > 0}
				<select id={fid} class="cfg-input" bind:value={drafts[field.name]}>
					<option value="">{field.required ? 'choose' : 'none'}</option>
					{#each field.node.kind === 'enum' ? field.node.choices : choicesFor(field.node, configEditor.values) as choice (String(choice))}
						<option value={String(choice)}>{String(choice)}</option>
					{/each}
				</select>
			{:else if field.node.kind === 'json'}
				<textarea
					id={fid}
					class="cfg-input"
					spellcheck="false"
					placeholder={'{}'}
					bind:value={drafts[field.name]}
				></textarea>
			{:else}
				<input
					id={fid}
					class="cfg-input"
					type="text"
					spellcheck="false"
					autocomplete="off"
					placeholder={field.node.kind === 'list' ? 'words separated by spaces' : ''}
					bind:value={drafts[field.name]}
				/>
			{/if}
			<p class="cfg-hint">{field.node.description}</p>
		</div>
	{/each}
	{#if error}
		<p class="cfg-error" role="alert">{error}</p>
	{/if}
	<div class="cfg-row">
		<button class="cfg-button cfg-button--on" type="submit">Add</button>
		<button class="cfg-button" type="button" onclick={oncancel}>Cancel</button>
	</div>
</form>
