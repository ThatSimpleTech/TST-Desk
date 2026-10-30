<script lang="ts">
	// A list or map of groups (TD-5203): models, hooks, language servers,
	// local servers, a tier's backups. Each entry is a collapsible card;
	// Remove asks once more before it goes. A new list item is filled in
	// through its form and appended; the form stays open, with what was typed,
	// until the daemon has written it. A map's entries are added on their own
	// page (models, MCP servers), and the note says which.
	import type { ConfigNode, JsonValue } from '../../protocol';
	import {
		configEditor,
		isOpen,
		resetConfigValue,
		saveConfigValue,
		setOpen,
	} from '../../config-editor.svelte.js';
	import { entryLabel, getAt, pathKey, type ConfigPath } from '../../config-editor.js';
	import Icon from '../Icon.svelte';
	import ConfigAddForm from './ConfigAddForm.svelte';
	import ConfigGroup from './ConfigGroup.svelte';

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

	const entryNode = $derived(node.items ?? node.entries);
	const def = $derived(configEditor.defs[entryNode?.ref ?? '']);
	const held = $derived(getAt(configEditor.values, path));
	const entries = $derived.by((): { key: string | number; value: JsonValue }[] => {
		if (Array.isArray(held)) return held.map((value, key) => ({ key, value }));
		if (held !== null && typeof held === 'object')
			return Object.entries(held).map(([key, value]) => ({ key, value }));
		return [];
	});
	const editable = $derived(!readonly && node.editable && configEditor.status === 'ready');
	const idBase = $derived(
		'cfg-entries-' + path.map((p) => String(p).replace(/[^A-Za-z0-9_-]/g, '_')).join('--'),
	);
	let adding = $state(false);
	let confirming = $state<string | null>(null);
	/** The add or remove this card list sent last, for its save state. */
	let lastEdit = $state<ConfigPath | null>(null);
	const lastSave = $derived(lastEdit ? configEditor.saves[pathKey(lastEdit)] : undefined);
	/** The item the open form sent, until the daemon has written it. */
	let pendingAdd = $state<ConfigPath | null>(null);

	function title(entry: { key: string | number; value: JsonValue }): string {
		return typeof entry.key === 'string' ? entry.key : entryLabel(entry.value, `Item ${entry.key + 1}`);
	}

	$effect(() => {
		if (pendingAdd !== null && configEditor.saves[pathKey(pendingAdd)]?.state === 'saved') {
			adding = false;
			pendingAdd = null;
		}
	});

	function add(value: Record<string, JsonValue>): void {
		lastEdit = [...path, entries.length];
		pendingAdd = lastEdit;
		saveConfigValue(lastEdit, value);
	}

	function remove(key: string | number): void {
		confirming = null;
		lastEdit = [...path, key];
		resetConfigValue(lastEdit);
	}
</script>

<div class:cfg-group={!flat}>
	{#if !flat}
		<span class="cfg-label">{name}</span>
		{#if node.description}<p class="cfg-hint">{node.description}</p>{/if}
	{/if}
	{#if !node.editable && node.note}
		<p class="cfg-note">{node.note}</p>
	{/if}
	{#each entries as entry (String(entry.key))}
		{@const entryPath = [...path, entry.key]}
		{@const open = visible !== null || isOpen(entryPath)}
		{#if visible === null || visible.has(pathKey(entryPath))}
			<div class="cfg-entry">
				<div class="cfg-entry-head">
					<button
						class="cfg-group-head"
						type="button"
						aria-expanded={open}
						onclick={() => setOpen(entryPath, !open)}
					>
						<span class="cfg-chevron" class:cfg-chevron--open={open}><Icon name="chevron-right" size={12} /></span>
						<span>{title(entry)}</span>
					</button>
					{#if editable && node.removable}
						{#if confirming === String(entry.key)}
							<span class="cfg-row">
								<button class="cfg-button cfg-button--danger" type="button" onclick={() => remove(entry.key)}
									>Remove</button
								>
								<button class="cfg-button" type="button" onclick={() => (confirming = null)}>Keep</button>
							</span>
						{:else}
							<button
								class="cfg-button cfg-button--quiet"
								type="button"
								aria-label={`Remove ${title(entry)}`}
								onclick={() => (confirming = String(entry.key))}><Icon name="trash" size={12} /></button
							>
						{/if}
					{/if}
				</div>
				{#if open && entryNode}
					<div class="cfg-entry-body">
						<ConfigGroup node={entryNode} path={entryPath} name={title(entry)} readonly={!editable} {visible} flat />
					</div>
				{/if}
			</div>
		{/if}
	{:else}
		<p class="cfg-muted">None yet.</p>
	{/each}
	{#if editable && node.addable && Array.isArray(held) && def}
		{#if adding}
			<ConfigAddForm
				{def}
				idPrefix={idBase}
				label={`Add to ${name}`}
				onsubmit={add}
				oncancel={() => {
					adding = false;
					pendingAdd = null;
				}}
			/>
		{:else}
			<div class="cfg-control">
				<button class="cfg-button" type="button" onclick={() => (adding = true)}>
					<Icon name="plus" size={12} /> Add
				</button>
			</div>
		{/if}
	{/if}
	{#if node.add_note}<p class="cfg-muted">{node.add_note}</p>{/if}
	{#if node.remove_note}<p class="cfg-muted">{node.remove_note}</p>{/if}
	{#if lastSave}
		<p
			class="cfg-status"
			class:cfg-status--saved={lastSave.state === 'saved'}
			class:cfg-status--error={lastSave.state === 'error'}
			aria-live="polite"
		>
			{#if lastSave.state === 'saving'}Saving…{:else if lastSave.state === 'saved'}Saved{:else}{lastSave.message}{/if}
		</p>
	{/if}
</div>
