<script lang="ts">
	// A list drawn from a fixed set (TD-5203), such as the tiers offered
	// consult or the read-only shell entries: one toggle per choice. The list
	// is saved in the set's own order, so toggling never reshuffles the file.
	import type { JsonValue } from '../../protocol';

	let {
		id,
		labelId,
		choices,
		value,
		disabled,
		onsave,
	}: {
		id: string;
		labelId: string;
		choices: JsonValue[];
		value: JsonValue | undefined;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	const chosen = $derived(Array.isArray(value) ? value : []);

	function toggle(choice: JsonValue): void {
		const next = chosen.includes(choice)
			? chosen.filter((c) => c !== choice)
			: choices.filter((c) => c === choice || chosen.includes(c));
		onsave(next);
	}
</script>

<div {id} class="cfg-segments" role="group" aria-labelledby={labelId}>
	{#each choices as choice (String(choice))}
		<button
			class="cfg-button"
			class:cfg-button--on={chosen.includes(choice)}
			type="button"
			aria-pressed={chosen.includes(choice)}
			{disabled}
			onclick={() => toggle(choice)}>{String(choice)}</button
		>
	{/each}
</div>
