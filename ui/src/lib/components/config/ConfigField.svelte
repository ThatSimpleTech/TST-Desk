<script lang="ts">
	// One config.yaml key (TD-5203): its name and unit, the control its kind
	// gets, what it does, its default, whether it differs from that default,
	// and how its last save went. Reset puts the default back. A key the file
	// writes through a YAML anchor or merge is shown, and edited by hand.
	import type { ConfigNode } from '../../protocol';
	import {
		configEditor,
		isLocked,
		resetConfigValue,
		saveConfigValue,
	} from '../../config-editor.svelte.js';
	import {
		boundsHint,
		choicesFor,
		controlFor,
		defaultFor,
		formatValue,
		getAt,
		isModified,
		LOCKED_NOTE,
		pathKey,
		type ConfigPath,
	} from '../../config-editor.js';
	import Icon from '../Icon.svelte';
	import ConfigChips from './ConfigChips.svelte';
	import ConfigChoice from './ConfigChoice.svelte';
	import ConfigInput from './ConfigInput.svelte';
	import ConfigJson from './ConfigJson.svelte';
	import ConfigList from './ConfigList.svelte';
	import ConfigSecrets from './ConfigSecrets.svelte';
	import ConfigToggle from './ConfigToggle.svelte';
	import ConfigUnion from './ConfigUnion.svelte';

	let {
		node,
		path,
		name,
		readonly = false,
	}: { node: ConfigNode; path: ConfigPath; name: string; readonly?: boolean } = $props();

	const key = $derived(pathKey(path));
	const id = $derived(
		'cfg-' + path.map((p) => String(p).replace(/[^A-Za-z0-9_-]/g, '_')).join('--'),
	);
	const labelId = $derived(`${id}-label`);
	const value = $derived(getAt(configEditor.values, path));
	const fallback = $derived(defaultFor(configEditor.defaults, path, node));
	// Inside a list item or map entry a key with no default is simply set or
	// not (a model's label, a tier's extra_body): Clear, not "Modified".
	const clearable = $derived(
		!(key in configEditor.defaults) &&
			fallback.has &&
			fallback.value === null &&
			value !== null &&
			value !== undefined,
	);
	const modified = $derived(isModified(value, fallback) && !clearable);
	const save = $derived(configEditor.saves[key]);
	const pending = $derived(save?.state === 'saving');
	const control = $derived(controlFor(node));
	const locked = $derived(isLocked(path));
	const disabled = $derived(
		readonly || locked || !node.editable || configEditor.status !== 'ready',
	);
	const canReset = $derived((modified || clearable) && node.resettable && !disabled);
	const bounds = $derived(boundsHint(node));
	const choices = $derived(
		node.kind === 'list' && node.items ? node.items.choices : choicesFor(node, configEditor.values),
	);

	// A reference that may name nothing offers "none"; the active model may not.
	const blank = $derived(
		node.nullable ? [null]
		: node.choices_from.length > 0 && node.resettable && node.default === '' ? ['']
		: [],
	);

	function onsave(next: Parameters<typeof saveConfigValue>[1]): void {
		saveConfigValue(path, next);
	}
</script>

<div class="cfg-field" data-config-key={key}>
	<div class="cfg-field-head">
		<span class="cfg-label">
			<!-- The badge sits outside the label, so the control's name stays the key's. -->
			<label id={labelId} for={id} class="cfg-label-text">
				<span>{name}</span>
				{#if node.unit}<span class="cfg-unit">{node.unit}</span>{/if}
			</label>
			{#if modified}<span class="cfg-badge">Modified</span>{/if}
		</span>
		{#if canReset && clearable}
			<button
				class="cfg-button cfg-button--quiet"
				type="button"
				aria-label={`Clear ${name}`}
				title="Remove this key from config.yaml"
				onclick={() => resetConfigValue(path)}><Icon name="x" size={12} /> Clear</button
			>
		{:else if canReset}
			<button
				class="cfg-button cfg-button--quiet"
				type="button"
				aria-label={`Reset ${name} to default`}
				title={`Reset to ${formatValue(fallback.value, node.unit)}`}
				onclick={() => resetConfigValue(path)}><Icon name="retry" size={12} /> Reset</button
			>
		{/if}
	</div>
	<div class="cfg-control">
		{#if control === 'toggle'}
			<ConfigToggle {id} {value} {disabled} {onsave} />
		{:else if control === 'choice' || control === 'select'}
			<ConfigChoice
				{id}
				{labelId}
				{choices}
				{value}
				{blank}
				segmented={control === 'choice'}
				{pending}
				{disabled}
				{onsave}
			/>
		{:else if control === 'number' || control === 'text' || control === 'textarea'}
			<ConfigInput {id} {node} {value} {pending} {disabled} {onsave} />
		{:else if control === 'chips'}
			<ConfigChips {id} {labelId} {choices} {value} {pending} {disabled} {onsave} />
		{:else if control === 'list'}
			<ConfigList {id} {labelId} {name} {node} {value} {pending} {disabled} {onsave} />
		{:else if control === 'union'}
			<ConfigUnion {id} {labelId} {name} {node} {value} {pending} {disabled} {onsave} />
		{:else if control === 'secrets'}
			<ConfigSecrets {id} {value} />
		{:else}
			<ConfigJson {id} {node} {value} {pending} {disabled} {onsave} />
		{/if}
	</div>
	<p class="cfg-hint">
		{node.description}
		{#if fallback.has}<span class="cfg-muted">Default: {formatValue(fallback.value, node.unit)}.</span>{/if}
		{#if bounds}<span class="cfg-muted">Range: {bounds}.</span>{/if}
	</p>
	{#if node.note}
		<p class="cfg-note">{node.note}</p>
	{/if}
	{#if configEditor.locked[key]}
		<p class="cfg-note">{LOCKED_NOTE}</p>
	{/if}
	<p
		class="cfg-status"
		class:cfg-status--saved={save?.state === 'saved'}
		class:cfg-status--error={save?.state === 'error'}
		aria-live="polite"
	>
		{#if save?.state === 'saving'}Saving…{:else if save?.state === 'saved'}Saved{:else if save?.state === 'error'}{save.message}{/if}
	</p>
</div>
