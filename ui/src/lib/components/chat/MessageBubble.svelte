<script lang="ts">
	// One conversation message (TD-1004, restyled TD-1603). Assistant messages
	// render full-width on the canvas — no bubble, no avatar — with a hairline
	// opening each new turn. User messages keep a right-aligned warm-tan
	// bubble at ≈80% max width. The streaming caret takes the accent.
	// Assistant messages render markdown; user messages render as plain text
	// with newlines preserved.
	import type { ChatMessage } from "../../chat-store";
	import Markdown from "./Markdown.svelte";

	let {
		message,
		first = false,
	}: {
		message: ChatMessage;
		/** First row of the conversation: suppresses the turn hairline. */
		first?: boolean;
	} = $props();
</script>

<div
	class="row"
	class:user={message.role === "user"}
	class:turn-start={message.role === "user" && !first}
>
	{#if message.role === "assistant"}
		<div class="assistant-msg">
			<Markdown text={message.text} />
			{#if !message.complete}<span class="cursor" aria-hidden="true">▍</span>{/if}
		</div>
	{:else}
		<div class="bubble">
			<p class="user-text">{message.text}</p>
		</div>
	{/if}
</div>

<style>
	.row {
		display: flex;
	}

	.row.user {
		justify-content: flex-end;
	}

	/* A hairline, not a bubble, marks where one turn ends and the next
	   begins. */
	.turn-start {
		margin-top: var(--space-2);
		padding-top: var(--space-5);
		border-top: var(--border-width) solid var(--color-hairline);
	}

	.assistant-msg {
		width: 100%;
		color: var(--color-ink);
	}

	.bubble {
		max-width: 80%;
		padding: var(--space-2) var(--space-4);
		border-radius: var(--radius-lg);
		border-bottom-right-radius: var(--radius-sm);
		background: var(--color-user-bubble);
		color: var(--color-ink);
		font-size: var(--text-base);
		line-height: var(--leading-normal);
	}

	.user-text {
		white-space: pre-wrap;
		word-break: break-word;
	}

	.cursor {
		display: inline-block;
		color: var(--color-accent);
		animation: blink 1s step-start infinite;
	}

	@keyframes blink {
		50% {
			opacity: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.cursor {
			animation: none;
		}
	}
</style>
