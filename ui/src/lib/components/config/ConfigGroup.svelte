<script lang="ts">
	// A group of keys (TD-5203): a nested section such as notify.slack, or one
	// list item or map entry. Collapsible unless `flat`. A group that may be
	// absent (a tier's reply_guard, a model's fast) is Off until turned on:
	// straight away when an empty one is valid, through its form otherwise.
	import type { ConfigNode, JsonValue } from '../../protocol';
	import {
		configEditor,
		isOpen,
		resetConfigValue,
		saveConfigValue,
		setOpen,
	} from '../../config-editor.svelte.js';
	import { getAt, pathKey, type ConfigPath } from '../../config-editor.js';
	import Icon from '../Icon.svelte';
	import ConfigAddForm from './ConfigAddForm.svelte';
	import ConfigNodeView from './ConfigNodeView.svelte';

	let {
		node,
		path,
		name,
		readonly = false,
		visible = null,
		flat = false,
	}: {
		node: ConfigNode;
		path: ConfigPath;
		name: string;
		readonly?: boolean;
		visible?: Set<string> | null;
		flat?: boolean;
	} = $props();

	const def = $derived(configEditor.defs[node.ref ?? '']);
	const value = $derived(getAt(configEditor.values, path));
	const off = $derived(node.nullable && (value === null || value === undefined));
	const open = $derived(flat || visible !== null || isOpen(path));
	const locked = $derived(readonly || !node.editable || configEditor.status !== 'ready');
	const save = $derived(configEditor.saves[pathKey(path)]);
	const headId = $derived(
		'cfg-group-' + path.map((p) => String(p).replace(/[^A-Za-z0-9_-]/g, '_')).join('--'),
	);
	let adding = $state(false);

	function turnOn(): void {
		if (def?.empty_ok) saveConfigValue(path, {});
		else adding = true;
	}

	// The form closes once the group is on; a refusal keeps it, with what was typed.
	$effect(() => {
		if (adding && !off) adding = false;
	});

	function add(entry: Record<string, JsonValue>): void {
		saveConfigValue(path, entry);
	}
</script>

{#if def}
	{#if off}
		<div class="cfg-field">
			<div class="cfg-field-head">
				<span class="cfg-label"><span>{name}</span><span class="cfg-unit">off</span></span>
				{#if !locked && !adding}
					<button class="cfg-button" type="button" onclick={turnOn}>Turn on</button>
				{/if}
			</div>
			<p class="cfg-hint">{node.description}</p>
			{#if adding}
				<ConfigAddForm {def} idPrefix={headId} label={`Turn on ${name}`} onsubmit={add} oncancel={() => (adding = false)} />
			{/if}
			{#if save?.state === 'error'}<p class="cfg-error" role="alert">{save.message}</p>{/if}
		</div>
	{:else}
		<div class:cfg-group={!flat} role="group" aria-labelledby={flat ? undefined : headId}>
			{#if !flat}
				<div class="cfg-field-head">
					<button
						id={headId}
						class="cfg-group-head"
						type="button"
						aria-expanded={open}
						onclick={() => setOpen(path, !open)}
					>
						<span class="cfg-chevron" class:cfg-chevron--open={open}><Icon name="chevron-right" size={12} /></span>
						<span class="cfg-label">{name}</span>
					</button>
					{#if node.nullable && !locked}
						<button
							class="cfg-button cfg-button--quiet"
							type="button"
							aria-label={`Turn off ${name}`}
							onclick={() => resetConfigValue(path)}>Turn off</button
						>
					{/if}
				</div>
				{#if node.description}<p class="cfg-hint">{node.description}</p>{/if}
				{#if save?.state === 'error'}<p class="cfg-error" role="alert">{save.message}</p>{/if}
			{/if}
			{#if open}
				{#each def.fields as field (field.name)}
					<ConfigNodeView
						node={field.node}
						path={[...path, field.name]}
						name={field.name}
						{readonly}
						{visible}
					/>
				{/each}
			{/if}
		</div>
	{/if}
{/if}
