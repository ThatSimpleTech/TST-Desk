<script lang="ts">
	// A command that may be one line or a list of arguments (TD-5203): the
	// embeddings and computer-use commands. The segments pick the shape the
	// next save writes; the value on screen is converted, not saved, until
	// the person edits it.
	import type { ConfigNode, JsonValue } from '../../protocol';
	import { joinWords, splitWords } from '../../config-editor.js';
	import ConfigInput from './ConfigInput.svelte';
	import ConfigList from './ConfigList.svelte';

	let {
		id,
		labelId,
		name,
		node,
		value,
		pending,
		disabled,
		onsave,
	}: {
		id: string;
		labelId: string;
		name: string;
		node: ConfigNode;
		value: JsonValue | undefined;
		pending: boolean;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	const textNode = $derived(node.variants.find((v) => v.kind === 'str') ?? null);
	const listNode = $derived(node.variants.find((v) => v.kind === 'list') ?? null);
	let asList = $state<boolean | null>(null);
	const listMode = $derived(asList ?? Array.isArray(value));
	const shown = $derived.by((): JsonValue => {
		if (listMode) return Array.isArray(value) ? value : splitWords(String(value ?? ''));
		return Array.isArray(value) ? joinWords(value) : (value ?? '');
	});
</script>

<div class="cfg-segments" role="radiogroup" aria-labelledby={labelId}>
	<button
		class="cfg-button"
		class:cfg-button--on={!listMode}
		type="button"
		role="radio"
		aria-checked={!listMode}
		{disabled}
		onclick={() => (asList = false)}>One line</button
	>
	<button
		class="cfg-button"
		class:cfg-button--on={listMode}
		type="button"
		role="radio"
		aria-checked={listMode}
		{disabled}
		onclick={() => (asList = true)}>Arguments</button
	>
</div>
{#if listMode && listNode}
	<ConfigList {id} {labelId} {name} node={listNode} value={shown} {pending} {disabled} {onsave} />
{:else if textNode}
	<ConfigInput {id} node={textNode} value={shown} {pending} {disabled} {onsave} />
{/if}
