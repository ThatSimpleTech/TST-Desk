<script lang="ts">
	// Queued messages (TD-1704): the rows for messages composed while a turn
	// held the loop. Each row is its own edit surface — typing in it replaces
	// the text that will be sent — and carries send-now (hand it over ahead of
	// the rows before it) and remove.
	//
	// The whole strip lives inside one `showQueue` guard, so an empty queue
	// contributes no element, no border and no reserved height above the
	// composer. The container is invisible because it does not exist.
	import { showQueue, type QueuedMessage } from "../../chat-queue";
	import Icon from "../Icon.svelte";

	let {
		queued,
		onsendnow,
		onedit,
		onremove,
	}: {
		queued: QueuedMessage[];
		onsendnow: (id: string) => void;
		onedit: (id: string, text: string) => void;
		onremove: (id: string) => void;
	} = $props();
</script>

{#if showQueue(queued)}
	<div class="queue" role="list" aria-label="Queued messages">
		{#each queued as row (row.id)}
			<div class="row" role="listitem">
				<!-- A textarea, not an input: an input silently drops the newlines
				     of a multi-line message the moment it round-trips. -->
				<textarea
					class="text"
					rows="1"
					aria-label="Queued message"
					value={row.text}
					oninput={(event) => onedit(row.id, event.currentTarget.value)}
				></textarea>
				<button
					class="act"
					type="button"
					title="Send now — run this next"
					aria-label="Send now"
					onclick={() => onsendnow(row.id)}
				>
					<Icon name="arrow-up" size={14} />
				</button>
				<button
					class="act"
					type="button"
					title="Remove from queue"
					aria-label="Remove from queue"
					onclick={() => onremove(row.id)}
				>
					<Icon name="x" size={14} />
				</button>
			</div>
		{/each}
	</div>
{/if}

<style>
	.queue {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		padding: 0 var(--space-4) var(--space-2);
	}

	/* Quieter than the composer card below it: these are waiting, not active. */
	.row {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) var(--space-1) var(--space-1) var(--space-3);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-lg);
	}

	.row:focus-within {
		border-color: var(--color-accent);
	}

	.text {
		flex: 1;
		resize: none;
		overflow-y: auto;
		max-height: calc(var(--space-16) + var(--space-4));
		padding: var(--space-1) 0;
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		line-height: var(--leading-normal);
		color: var(--color-ink-secondary);
		background: transparent;
		border: none;
		outline: none;
	}

	.act {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		width: var(--space-6);
		height: var(--space-6);
		color: var(--color-ink-muted);
		background: transparent;
		border: none;
		border-radius: var(--radius-full);
		cursor: pointer;
		transition:
			color var(--transition-fast),
			background var(--transition-fast);
	}

	.act:hover {
		color: var(--color-ink);
		background: var(--color-lifted);
	}

	.act:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}
</style>
