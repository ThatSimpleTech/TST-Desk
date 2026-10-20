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
	import { formatWorkerTranscript } from '../../worker-transcript';

	// `complete` is the row's: a call on a finished row never shimmers, even
	// with no result, since a restart that cut it logs none (TD-5144).
	let {
		messageId,
		block,
		complete,
	}: { messageId: string; block: ToolBlock; complete: boolean } = $props();

	const live = $derived(isToolLive(block, complete));
	const expanded = $derived(isToolExpanded(messageId, block, complete));
	const label = $derived(toolLabel(block));
	// A delegate's worker steps, kept out of the model's context (TD-5117).
	const transcript = $derived(
		block.transcript !== undefined && block.transcript.length > 0
			? formatWorkerTranscript(block.transcript)
			: null
	);

	function argsText(): string {
		try {
			return JSON.stringify(block.arguments, null, 2);
		} catch {
			return String(block.arguments);
		}
	}
</script>

<Disclosure {expanded} {live} {label} ontoggle={() => toggleTool(messageId, block, complete)}>
	{argsText()}{#if block.output}
		{'\n\n'}{block.output}{/if}{#if transcript}
		{'\n\n── Worker transcript ──\n\n'}{transcript}{/if}
</Disclosure>
