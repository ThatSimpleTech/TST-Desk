<script lang="ts">
	// A boolean key (TD-5203): a switch that saves on click.
	import type { JsonValue } from '../../protocol';

	let {
		id,
		value,
		disabled,
		onsave,
	}: {
		id: string;
		value: JsonValue | undefined;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	const on = $derived(value === true);
</script>

<button
	{id}
	class="cfg-button"
	class:cfg-button--on={on}
	type="button"
	role="switch"
	aria-checked={on}
	{disabled}
	onclick={() => onsave(!on)}>{on ? 'On' : 'Off'}</button
>
