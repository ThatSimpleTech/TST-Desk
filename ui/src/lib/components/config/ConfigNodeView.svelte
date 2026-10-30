<script lang="ts">
	// One node of the config tree (TD-5203): a group, a list of groups, or a
	// single key, picked by the node's kind. Hidden when a search leaves it out.
	import type { ConfigNode } from '../../protocol';
	import { controlFor, pathKey, type ConfigPath } from '../../config-editor.js';
	import ConfigEntries from './ConfigEntries.svelte';
	import ConfigField from './ConfigField.svelte';
	import ConfigGroup from './ConfigGroup.svelte';

	let {
		node,
		path,
		name,
		readonly = false,
		visible = null,
	}: {
		node: ConfigNode;
		path: ConfigPath;
		name: string;
		readonly?: boolean;
		visible?: Set<string> | null;
	} = $props();

	const control = $derived(controlFor(node));
	const shown = $derived(visible === null || visible.has(pathKey(path)));
	const locked = $derived(readonly || !node.editable);
</script>

{#if shown}
	{#if control === 'group'}
		<ConfigGroup {node} {path} {name} readonly={locked} {visible} />
	{:else if control === 'entries'}
		<ConfigEntries {node} {path} {name} readonly={locked} {visible} />
	{:else}
		<ConfigField {node} {path} {name} readonly={locked} />
	{/if}
{/if}
