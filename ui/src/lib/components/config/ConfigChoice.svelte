<script lang="ts">
	// One of a fixed set (TD-5203): segments for a few short choices, a select
	// otherwise. Saves on change. `blank` comes first: "not set" (null) for a
	// nullable key, "none" ("") for a reference that may name nothing. A value
	// the file has that is no longer a choice still shows, so it is never hidden.
	import type { JsonValue } from '../../protocol';

	let {
		id,
		labelId,
		choices,
		value,
		blank,
		segmented,
		disabled,
		onsave,
	}: {
		id: string;
		labelId: string;
		choices: JsonValue[];
		value: JsonValue | undefined;
		blank: JsonValue[];
		segmented: boolean;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	const current = $derived(value ?? null);
	const options = $derived.by(() => {
		const all: JsonValue[] = [...blank, ...choices];
		if (!all.some((c) => c === current)) all.push(current);
		return all;
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
		{id}
		class="cfg-input"
		{disabled}
		value={String(options.indexOf(current))}
		onchange={(e) => pick(Number(e.currentTarget.value))}
	>
		{#each options as choice, i (i)}
			<option value={String(i)}>{label(choice)}</option>
		{/each}
	</select>
{/if}
