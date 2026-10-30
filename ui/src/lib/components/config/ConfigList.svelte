<script lang="ts">
	// A list of words (TD-5203): one row per item, each removable, and a box
	// to add one. Every change saves the whole list. An emptied row is a
	// removal; an item that does not fit the key's rules is not sent.
	import type { ConfigNode, JsonValue } from '../../protocol';
	import { parseNumber, parseText } from '../../config-editor.js';
	import Icon from '../Icon.svelte';

	let {
		id,
		labelId,
		node,
		value,
		revision,
		disabled,
		onsave,
	}: {
		id: string;
		labelId: string;
		node: ConfigNode;
		value: JsonValue | undefined;
		/** The store's answer count: rows redraw from the stored list on each. */
		revision: number;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	const items = $derived(Array.isArray(value) ? value : []);
	const itemNode = $derived(node.items);
	let adding = $state('');
	let error = $state('');

	function parse(text: string) {
		if (itemNode === null) return { ok: true as const, value: text as JsonValue };
		return itemNode.kind === 'int' || itemNode.kind === 'float'
			? parseNumber(text, itemNode)
			: parseText(text, itemNode);
	}

	function replace(index: number, text: string): void {
		if (text === String(items[index])) return;
		if (text.trim() === '') {
			remove(index);
			return;
		}
		const parsed = parse(text);
		if (!parsed.ok) {
			error = parsed.error;
			return;
		}
		error = '';
		onsave(items.map((item, i) => (i === index ? parsed.value : item)));
	}

	function remove(index: number): void {
		error = '';
		onsave(items.filter((_, i) => i !== index));
	}

	function add(): void {
		if (adding.trim() === '') return;
		const parsed = parse(adding.trim());
		if (!parsed.ok) {
			error = parsed.error;
			return;
		}
		error = '';
		adding = '';
		onsave([...items, parsed.value]);
	}
</script>

<div class="cfg-rows" role="group" aria-labelledby={labelId}>
	{#each items as item, i (`${revision}:${i}`)}
		<div class="cfg-row">
			<input
				class="cfg-input"
				type="text"
				spellcheck="false"
				autocomplete="off"
				aria-label={`Item ${i + 1}`}
				value={String(item)}
				{disabled}
				onblur={(e) => replace(i, e.currentTarget.value)}
				onkeydown={(e) => {
					if (e.key === 'Enter') replace(i, e.currentTarget.value);
					if (e.key === 'Escape') e.currentTarget.value = String(item);
				}}
			/>
			<button
				class="cfg-button cfg-button--quiet"
				type="button"
				aria-label={`Remove ${String(item)}`}
				{disabled}
				onclick={() => remove(i)}><Icon name="x" size={12} /></button
			>
		</div>
	{/each}
	{#if items.length === 0}
		<p class="cfg-muted">None.</p>
	{/if}
	<div class="cfg-row">
		<input
			{id}
			class="cfg-input"
			type="text"
			spellcheck="false"
			autocomplete="off"
			placeholder="Add an item"
			bind:value={adding}
			{disabled}
			onkeydown={(e) => {
				if (e.key === 'Enter') add();
			}}
		/>
		<button class="cfg-button" type="button" {disabled} onclick={add}>Add</button>
	</div>
	{#if error}
		<p class="cfg-error" role="alert">{error}</p>
	{/if}
</div>
