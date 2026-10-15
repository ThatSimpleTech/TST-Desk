<script lang="ts">
	// The files one send_file call put in the chat (TD-5144, design §6.3).
	// Sits right after the call's fold, outside it, so the cards stay when
	// the fold collapses. While the call runs it is one skeleton naming the
	// first file; a refused call shows nothing, since the fold carries the
	// refusal. The caption shows once for the group.
	//
	// The skeleton needs the turn to be live. A restart that cuts the call
	// off seals the turn with no tool_result for it, so a call with no
	// status on a finished row will never send: it says so instead of
	// "Sending…" forever (TD-5144 review finding 28).
	import type { ToolBlock } from "../../chat-store";
	import { sendingText } from "../../sent-files";
	import FileCard from "./FileCard.svelte";

	let {
		block,
		live = true,
	}: {
		block: ToolBlock;
		/** The row's turn is still running (the message is not complete). */
		live?: boolean;
	} = $props();

	const files = $derived(block.files ?? []);
	const unfinished = $derived(block.status === undefined && files.length === 0);
	const sending = $derived(unfinished && live ? sendingText(block.arguments.files) : null);
	const cut = $derived(unfinished && !live);
	const caption = $derived(files.find((f) => f.caption !== null)?.caption ?? null);
</script>

{#if block.status !== "error"}
	{#if files.length > 0}
		<div class="file-cards">
			{#each files as file (file.artifactId)}
				<FileCard {file} />
			{/each}
			{#if caption !== null}
				<p class="caption">{caption}</p>
			{/if}
		</div>
	{:else if sending !== null}
		<div class="file-cards">
			<div class="skeleton" role="status">
				<span class="kind" aria-hidden="true"></span>
				<span class="line">{sending}</span>
			</div>
		</div>
	{:else if cut}
		<p class="not-sent">Not sent: the turn was cut off.</p>
	{/if}
{/if}

<style>
	.file-cards {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		margin: var(--space-2) 0 var(--space-3);
		max-width: 36rem;
	}

	/* At most two lines, muted (§6.4). */
	.caption {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.not-sent {
		margin: var(--space-2) 0 var(--space-3);
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
	}

	.skeleton {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border: var(--border-width) dashed var(--color-hairline);
		border-radius: var(--radius-md);
		color: var(--color-ink-secondary);
		font-size: var(--text-sm);
	}

	.skeleton .kind {
		flex: 0 0 40px;
		height: 40px;
		border-radius: var(--radius-sm);
		background: var(--color-sunken);
		animation: pulse 1.6s ease-in-out infinite;
	}

	.line {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	@keyframes pulse {
		50% {
			opacity: 0.5;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.skeleton .kind {
			animation: none;
		}
	}
</style>
