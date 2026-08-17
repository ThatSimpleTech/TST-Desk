<script lang="ts">
	// One conversation message (TD-1004, restyled TD-1603). Assistant messages
	// render full-width on the canvas — no bubble, no avatar — with a hairline
	// opening each new turn. User messages keep a right-aligned warm-tan
	// bubble at ≈80% max width. The streaming caret takes the accent.
	//
	// Completed assistant messages carry a hover-only action bar (TD-1606):
	// copy the markdown source, retry (resend the last user message), and the
	// message timestamp. The bar also surfaces on focus-within so the buttons
	// are keyboard-reachable with visible focus.
	//
	// A user row that carried attachments shows them as chips (TD-1709).
	import type { ChatMessage } from "../../chat-store";
	import Icon from "../Icon.svelte";
	import AttachmentChips from "./AttachmentChips.svelte";
	import Markdown from "./Markdown.svelte";

	let {
		message,
		first = false,
		turnLive = false,
		onretry,
	}: {
		message: ChatMessage;
		/** First row of the conversation: suppresses the turn hairline. */
		first?: boolean;
		/** A turn is in flight: retry stands down (TD-1606). */
		turnLive?: boolean;
		onretry?: () => void;
	} = $props();

	let copied = $state(false);
	let copiedTimer: ReturnType<typeof setTimeout> | null = null;

	function copyMarkdown(): void {
		navigator.clipboard
			.writeText(message.text)
			.then(() => {
				copied = true;
				if (copiedTimer !== null) clearTimeout(copiedTimer);
				copiedTimer = setTimeout(() => {
					copied = false;
				}, 1500);
			})
			.catch(() => {
				// Clipboard unavailable (permissions) — nothing to offer.
			});
	}

	/** Local HH:MM — display-only hover detail, not data. */
	function formatClock(at: number): string {
		const d = new Date(at);
		const hh = String(d.getHours()).padStart(2, "0");
		const mm = String(d.getMinutes()).padStart(2, "0");
		return `${hh}:${mm}`;
	}
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
			{#if message.complete}
				<div class="actions">
					<button
						class="action"
						type="button"
						title={copied ? "Copied" : "Copy markdown"}
						aria-label="Copy message as markdown"
						onclick={copyMarkdown}
					>
						<Icon name={copied ? "check" : "copy"} size={14} />
					</button>
					<button
						class="action"
						type="button"
						title={turnLive ? "Retry is available when the turn ends" : "Retry — resend your last message"}
						aria-label="Retry: resend your last message"
						disabled={turnLive}
						onclick={() => onretry?.()}
					>
						<Icon name="retry" size={14} />
					</button>
					<span class="stamp">{formatClock(message.at)}</span>
				</div>
			{/if}
		</div>
	{:else}
		<div class="bubble">
			{#if message.text !== ""}<p class="user-text">{message.text}</p>{/if}
			{#if message.attachments !== undefined}
				<!-- TD-1709: the row shows what went with it. No remove control —
				     a message already sent cannot lose a file it carried. -->
				<div class="sent-attachments">
					<AttachmentChips chips={message.attachments} label="Files sent with this message" />
				</div>
			{/if}
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

	/* Hover-only action bar; focus-within keeps it keyboard-honest. The slot
	   height is reserved so the reveal never reflows the transcript. */
	.actions {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		height: calc(var(--space-6) + var(--space-1));
		margin-top: var(--space-1);
		opacity: 0;
		transition: opacity var(--transition-fast);
	}

	.assistant-msg:hover .actions,
	.assistant-msg:focus-within .actions {
		opacity: 1;
	}

	.action {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-1);
		color: var(--color-ink-muted);
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
	}

	.action:hover:not(:disabled) {
		color: var(--color-ink);
		background: var(--color-sunken);
	}

	.action:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

	.action:disabled {
		opacity: 0.45;
		cursor: default;
	}

	.stamp {
		margin-left: var(--space-1);
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-muted);
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

	/* Only spaced when there is text above it — an attachment-only message
	   should not open with a gap. */
	.user-text + .sent-attachments {
		margin-top: var(--space-2);
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
