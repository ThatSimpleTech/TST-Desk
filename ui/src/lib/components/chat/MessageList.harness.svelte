<script lang="ts">
	// Test-only mount surface for TD-1012. The list takes a `messages`
	// array that has to grow reactively; a .ts file cannot hold $state.
	import { onMount } from "svelte";
	import MessageList from "./MessageList.svelte";
	import type { ChatMessage } from "../../chat-store";

	let {
		onready,
	}: {
		onready: (api: { seed: (n: number) => void; append: (t: string) => void }) => void;
	} = $props();

	let messages: ChatMessage[] = $state([]);

	function append(text: string): void {
		messages.push({
			id: `m${messages.length + 1}`,
			role: messages.length % 2 === 0 ? "user" : "assistant",
			text,
			complete: true,
			at: Date.now(),
		});
	}

	function seed(count: number): void {
		messages = Array.from({ length: count }, (_, i) => ({
			id: `m${i + 1}`,
			role: i % 2 === 0 ? "user" : "assistant",
			text: `message ${i + 1}`,
			complete: true,
			at: Date.now(),
		}));
	}

	onMount(() => {
		onready({ seed, append });
	});
</script>

<div class="frame">
	<MessageList {messages} />
</div>

<style>
	.frame {
		width: 600px;
		height: 400px;
	}
</style>
