<script lang="ts">
	// One tool call, folded the same way as thinking (TD-1902).
	import type { ToolBlock } from '../../chat-store';
	import {
		isToolExpanded,
		isToolLive,
		toggleTool,
		toolLabel,
	} from '../../reasoning-disclosure.svelte.js';
	import Disclosure from './Disclosure.svelte';

	let { messageId, block }: { messageId: string; block: ToolBlock } = $props();

	const live = $derived(isToolLive(block));
	const expanded = $derived(isToolExpanded(messageId, block));
	const label = $derived(toolLabel(block));

	function argsText(): string {
		try {
			return JSON.stringify(block.arguments, null, 2);
		} catch {
			return String(block.arguments);
		}
	}
</script>

<Disclosure {expanded} {live} {label} ontoggle={() => toggleTool(messageId, block)}>
	{argsText()}{#if block.output}
		{'\n\n'}{block.output}{/if}
</Disclosure>
