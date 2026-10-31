<script lang="ts">
	// One top-level section of config.yaml (TD-5203), collapsed until opened
	// or until a search finds something in it. The header counts the keys
	// that differ from a fresh install; "Also in" goes to a page that edits
	// some of the same keys its own way.
	import type { ConfigField as FieldDef } from '../../protocol';
	import { configEditor, isOpen, setOpen } from '../../config-editor.svelte.js';
	import { setSection } from '../../settings.svelte.js';
	import {
		PAGE_LABEL,
		SECTION_PAGES,
		changedCount,
		controlFor,
	} from '../../config-editor.js';
	import Icon from '../Icon.svelte';
	import ConfigEntries from './ConfigEntries.svelte';
	import ConfigField from './ConfigField.svelte';
	import ConfigGroup from './ConfigGroup.svelte';

	let { field, visible = null }: { field: FieldDef; visible?: Set<string> | null } = $props();

	const path = $derived([field.name]);
	const open = $derived(visible !== null || isOpen(path));
	const control = $derived(controlFor(field.node));
	const pages = $derived(SECTION_PAGES[field.name] ?? []);
	const changed = $derived(changedCount(field.name, configEditor.defaults, configEditor.values));
	const headId = $derived(`cfg-section-${field.name}`);
</script>

<section class="cfg-section" aria-labelledby={headId}>
	<button
		id={headId}
		class="cfg-section-head"
		type="button"
		aria-expanded={open}
		onclick={() => setOpen(path, !open)}
	>
		<span class="cfg-chevron" class:cfg-chevron--open={open}><Icon name="chevron-right" size={12} /></span>
		<span class="cfg-section-title">{field.node.title || field.name}</span>
		<span class="cfg-key">{field.name}</span>
		{#if changed > 0}<span class="cfg-badge">{changed} changed</span>{/if}
	</button>
	{#if open}
		<div class="cfg-section-body">
			<p class="cfg-hint">{field.node.description}</p>
			{#if pages.length > 0}
				<div class="cfg-links">
					{#each pages as page (page)}
						<button class="cfg-link" type="button" onclick={() => setSection(page)}
							>Also in {PAGE_LABEL[page] ?? page} <Icon name="chevron-right" size={12} /></button
						>
					{/each}
				</div>
			{/if}
			{#if control === 'group'}
				<ConfigGroup node={field.node} {path} name={field.name} {visible} flat />
			{:else if control === 'entries'}
				<ConfigEntries node={field.node} {path} name={field.name} readonly={!field.node.editable} {visible} flat />
			{:else}
				<ConfigField node={field.node} {path} name={field.name} readonly={!field.node.editable} />
			{/if}
		</div>
	{/if}
</section>
