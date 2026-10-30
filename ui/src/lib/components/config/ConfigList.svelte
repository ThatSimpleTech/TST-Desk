<script lang="ts">
	// A list of words (TD-5203): one row per item, each removable, and a box
	// to add one. Every change saves the whole list. An emptied row is a
	// removal; an item that does not fit the key's rules is not sent.
	//
	// What is typed in a row stays a draft until Enter or leaving the row;
	// Escape drops it. Rows are keyed by position, so an answer to any other
	// save never redraws one: a row being typed in keeps its focus and its
	// text. A change builds on the list as this control last sent it, so a
	// second change made before the first is answered keeps the first. Once
	// this key's saves are answered the rows show the stored list again: the
	// new one, or the old one after a refusal.
	import type { ConfigNode, JsonValue } from '../../protocol';
	import { parseNumber, parseText } from '../../config-editor.js';
	import Icon from '../Icon.svelte';

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
		/** A save of this key is out or queued. */
		pending: boolean;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	/** The list as this control last sent it, until its saves are answered. */
	let sent = $state<JsonValue[] | null>(null);
	const items = $derived(sent ?? (Array.isArray(value) ? value : []));
	const itemNode = $derived(node.items);
	/** Text typed in a row and not yet committed, by row. */
	let drafts = $state<Record<number, string>>({});
	let adding = $state('');
	let error = $state('');

	$effect(() => {
		if (!pending && sent !== null) sent = null;
	});

	function parse(text: string) {
		if (itemNode === null) return { ok: true as const, value: text as JsonValue };
		return itemNode.kind === 'int' || itemNode.kind === 'float'
			? parseNumber(text, itemNode)
			: parseText(text, itemNode);
	}

	function save(next: JsonValue[]): void {
		sent = next;
		onsave(next);
	}

	function commit(index: number): void {
		const text = drafts[index];
		if (text === undefined) return;
		delete drafts[index];
		if (index >= items.length || text === String(items[index])) return;
		if (text.trim() === '') {
			remove(index);
			return;
		}
		const parsed = parse(text);
		if (!parsed.ok) {
			drafts[index] = text;
			error = parsed.error;
			return;
		}
		error = '';
		save(items.map((item, i) => (i === index ? parsed.value : item)));
	}

	function remove(index: number): void {
		error = '';
		// Positions shift: a draft left on a later row would land on its neighbour.
		drafts = {};
		save(items.filter((_, i) => i !== index));
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
		save([...items, parsed.value]);
	}

	/** Escape drops what was typed; with nothing to drop it closes Settings as usual. */
	function cancel(event: KeyboardEvent, dropped: boolean): void {
		if (!dropped) return;
		event.stopPropagation();
		event.preventDefault();
		error = '';
	}
</script>

<div class="cfg-rows" role="group" aria-labelledby={labelId}>
	{#each items as item, i (i)}
		<div class="cfg-row">
			<input
				class="cfg-input"
				type="text"
				spellcheck="false"
				autocomplete="off"
				aria-label={`Item ${i + 1}`}
				value={drafts[i] ?? String(item)}
				{disabled}
				oninput={(e) => (drafts[i] = e.currentTarget.value)}
				onblur={(e) => {
					// A row taken out of the page while focused still reports a blur.
					if (e.currentTarget.isConnected) commit(i);
				}}
				onkeydown={(e) => {
					if (e.key === 'Enter') commit(i);
					else if (e.key === 'Escape') {
						const had = drafts[i] !== undefined;
						delete drafts[i];
						cancel(e, had);
					}
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
				else if (e.key === 'Escape') {
					const had = adding !== '';
					adding = '';
					cancel(e, had);
				}
			}}
		/>
		<button class="cfg-button" type="button" aria-label={`Add to ${name}`} {disabled} onclick={add}
			>Add</button
		>
	</div>
	{#if error}
		<p class="cfg-error" role="alert">{error}</p>
	{/if}
</div>
