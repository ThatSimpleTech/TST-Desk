<script lang="ts">
	// A list drawn from a fixed set (TD-5203), such as the tiers offered
	// consult or the read-only shell entries: one toggle per choice. The list
	// is saved in the set's own order, so toggling never reshuffles the file.
	// A toggle builds on the list as this control last sent it, so a second
	// click before the first is answered keeps the first; once this key's
	// saves are answered the chips show the stored list again.
	import type { JsonValue } from '../../protocol';

	let {
		id,
		labelId,
		choices,
		value,
		pending,
		disabled,
		onsave,
	}: {
		id: string;
		labelId: string;
		choices: JsonValue[];
		value: JsonValue | undefined;
		/** A save of this key is out or queued. */
		pending: boolean;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	let sent = $state<JsonValue[] | null>(null);
	const chosen = $derived(sent ?? (Array.isArray(value) ? value : []));

	$effect(() => {
		if (!pending && sent !== null) sent = null;
	});

	function toggle(choice: JsonValue): void {
		const next = chosen.includes(choice)
			? chosen.filter((c) => c !== choice)
			: choices.filter((c) => c === choice || chosen.includes(c));
		sent = next;
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
