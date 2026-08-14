<script lang="ts">
	// One conversation message (TD-1004). Assistant messages render markdown;
	// user messages render as plain text with newlines preserved. The cursor
	// marks an assistant message that is still streaming.
	import type { ChatMessage } from "../../chat-store";
	import Markdown from "./Markdown.svelte";

	let { message }: { message: ChatMessage } = $props();
</script>

<div class="row" class:user={message.role === "user"}>
	<div class="bubble" class:user={message.role === "user"} class:assistant={message.role === "assistant"}>
		{#if message.role === "assistant"}
			<Markdown text={message.text} />
			{#if !message.complete}<span class="cursor" aria-hidden="true">▍</span>{/if}
		{:else}
			<p class="user-text">{message.text}</p>
		{/if}
	</div>
</div>

<style>
	.row {
		display: flex;
	}

	.row.user {
		justify-content: flex-end;
	}

	.bubble {
		max-width: 85%;
		padding: var(--space-3) var(--space-4);
		border-radius: var(--radius-lg);
		font-size: var(--text-base);
		line-height: var(--leading-normal);
	}

	.bubble.assistant {
		color: var(--color-text);
		background: var(--color-bg-subtle);
		border-bottom-left-radius: var(--radius-sm);
	}

	.bubble.user {
		color: var(--color-accent-text);
		background: var(--color-accent);
		border-bottom-right-radius: var(--radius-sm);
	}

	.user-text {
		white-space: pre-wrap;
		word-break: break-word;
	}

	.cursor {
		display: inline-block;
		color: var(--color-text-muted);
		animation: blink 1s step-start infinite;
	}

	@keyframes blink {
		50% {
			opacity: 0;
		}
	}
</style>
