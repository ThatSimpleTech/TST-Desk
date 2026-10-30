<script lang="ts">
	// One of a fixed set (TD-5203): segments for a few short choices, a select
	// otherwise. A click saves at once. `blank` comes first: "not set" (null)
	// for a nullable key, "none" ("") for a reference that may name nothing.
	// A value the file has that is no longer a choice still shows, so it is
	// never hidden.
	//
	// A select steps through its options as the arrow keys move, and some
	// engines report each step as a change; saving each would write every
	// option on the way (and switch the live model on each, for the active
	// one). A choice made with the keys waits until they rest, Enter, or
	// leaving the select. Once this key's save is answered, the select shows
	// the stored choice: the new one, or the old one again after a refusal.
	import type { JsonValue } from '../../protocol';

	let {
		id,
		labelId,
		choices,
		value,
		blank,
		segmented,
		pending,
		disabled,
		onsave,
	}: {
		id: string;
		labelId: string;
		choices: JsonValue[];
		value: JsonValue | undefined;
		blank: JsonValue[];
		segmented: boolean;
		/** A save of this key is out or queued. */
		pending: boolean;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	/** How long the keys must rest before a choice made with them is saved. */
	const KEYS_REST_MS = 600;

	const current = $derived(value ?? null);
	const options = $derived.by(() => {
		const all: JsonValue[] = [...blank, ...choices];
		if (!all.some((c) => c === current)) all.push(current);
		return all;
	});
	let select = $state<HTMLSelectElement | null>(null);
	/** An option picked with the keys and not saved yet. */
	let held = $state<number | null>(null);
	let keyed = false;
	let rest: ReturnType<typeof setTimeout> | null = null;

	$effect(() => {
		const stored = String(options.indexOf(current));
		if (!pending && held === null && select !== null && select.value !== stored) {
			select.value = stored;
		}
	});

	$effect(() => () => {
		if (rest !== null) clearTimeout(rest);
	});

	function label(choice: JsonValue): string {
		if (choice === null) return 'not set';
		if (choice === '') return 'none';
		return String(choice);
	}

	function pick(index: number): void {
		const choice = options[index];
		if (choice !== current) onsave(choice);
	}

	function commitHeld(): void {
		if (rest !== null) clearTimeout(rest);
		rest = null;
		if (held === null) return;
		const index = held;
		held = null;
		pick(index);
	}

	function onchange(event: Event & { currentTarget: HTMLSelectElement }): void {
		const index = Number(event.currentTarget.value);
		if (!keyed) {
			pick(index);
			return;
		}
		keyed = false;
		held = index;
		if (rest !== null) clearTimeout(rest);
		rest = setTimeout(commitHeld, KEYS_REST_MS);
	}

	function onkeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			commitHeld();
		} else if (event.key === 'Escape') {
			if (held === null) return;
			// The held option is dropped; Settings stays open.
			event.stopPropagation();
			event.preventDefault();
			if (rest !== null) clearTimeout(rest);
			rest = null;
			held = null;
		} else if (event.key !== 'Tab' && event.key !== 'Shift') {
			keyed = true;
		}
	}
</script>

{#if segmented}
	<div {id} class="cfg-segments" role="radiogroup" aria-labelledby={labelId}>
		{#each options as choice, i (i)}
			<button
				class="cfg-button"
				class:cfg-button--on={choice === current}
				type="button"
				role="radio"
				aria-checked={choice === current}
				{disabled}
				onclick={() => pick(i)}>{label(choice)}</button
			>
		{/each}
	</div>
{:else}
	<select
		bind:this={select}
		{id}
		class="cfg-input"
		{disabled}
		value={String(options.indexOf(current))}
		{onchange}
		{onkeydown}
		onblur={commitHeld}
	>
		{#each options as choice, i (i)}
			<option value={String(i)}>{label(choice)}</option>
		{/each}
	</select>
{/if}
