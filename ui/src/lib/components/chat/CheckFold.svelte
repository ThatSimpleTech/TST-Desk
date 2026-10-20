<script lang="ts">
	// A done-check on the reply it judged (TD-5124). Folded like a tool
	// call; a failure the turn ended on, or a check that could not run,
	// opens on its own so the reason is on screen.
	import type { CheckBlock } from '../../chat-message';
	import { checkBody, checkLabel, checkOpenByDefault } from '../../stop-check';
	import Disclosure from './Disclosure.svelte';

	let { check }: { check: CheckBlock } = $props();

	let toggled = $state<boolean | null>(null);
	const expanded = $derived(toggled ?? checkOpenByDefault(check));
</script>

<div class="check" class:failing={check.outcome !== 'passed'} role="group" aria-label="Done-check">
	<Disclosure {expanded} label={checkLabel(check)} ontoggle={() => (toggled = !expanded)}>
		{checkBody(check)}
	</Disclosure>
</div>

<style>
	.check.failing :global(.summary .label) {
		color: var(--color-ink);
	}
</style>
