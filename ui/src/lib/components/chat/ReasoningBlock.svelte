<script lang="ts">
	// A reasoning model's thinking, folded (TD-1902). Sits above the answer
	// in the same message row. Chrome lives in Disclosure so tool folds
	// share the same treatment.
	import type { ChatMessage } from '../../chat-store';
	import {
		isExpanded,
		isThinkingLive,
		thoughtLabel,
		toggle,
	} from '../../reasoning-disclosure.svelte.js';
	import Disclosure from './Disclosure.svelte';

	let { message }: { message: ChatMessage } = $props();

	const live = $derived(isThinkingLive(message));
	const expanded = $derived(isExpanded(message));
	const label = $derived(thoughtLabel(message));
</script>

<Disclosure {expanded} {live} {label} ontoggle={() => toggle(message)}>
	{message.reasoning}
</Disclosure>

