<script lang="ts">
	// Held messages (TD-5155): what the daemon had queued when it stopped.
	// They came back unsent, and each waits for the person: Send hands it to
	// the daemon as a new turn, Remove drops it. The text is read-only here
	// because it is the daemon's copy; Remove and a new message replace it.
	//
	// Like the queue below it, the strip does not exist while nothing is held.
	import { droppedNote, type HeldMessage } from "../../chat-held";
	import Icon from "../Icon.svelte";

	let {
		held,
		disabled = false,
		onsend,
		onremove,
	}: {
		held: HeldMessage[];
		disabled?: boolean;
		onsend: (id: string) => void;
		onremove: (id: string) => void;
	} = $props();
</script>

{#if held.length > 0}
	<div class="held" role="list" aria-label="Held messages">
		<p class="label">Kept through a restart, not sent yet</p>
		{#each held as row (row.id)}
			{@const note = droppedNote(row.imagesDropped)}
			<div class="row" role="listitem">
				<div class="body">
					<p class="text">{row.text}</p>
					{#if note !== null}
						<p class="note">{note}</p>
					{/if}
				</div>
				<button
					class="act"
					type="button"
					title="Send this message now"
					aria-label="Send held message"
					disabled={disabled || row.pending}
					onclick={() => onsend(row.id)}
				>
					<Icon name="arrow-up" size={14} />
				</button>
				<button
					class="act"
					type="button"
					title="Remove without sending"
					aria-label="Remove held message"
					disabled={disabled || row.pending}
					onclick={() => onremove(row.id)}
				>
					<Icon name="x" size={14} />
				</button>
			</div>
		{/each}
	</div>
{/if}

<style>
	.held {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		padding: 0 var(--space-4) var(--space-2);
	}

	.label {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.row {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) var(--space-1) var(--space-1) var(--space-3);
		background: var(--color-sunken);
		border: var(--border-width) dashed var(--color-hairline);
		border-radius: var(--radius-lg);
	}

	.body {
		flex: 1;
		min-width: 0;
	}

	.text {
		margin: 0;
		padding: var(--space-1) 0;
		max-height: calc(var(--space-16) + var(--space-4));
		overflow-y: auto;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		font-size: var(--text-sm);
		line-height: var(--leading-normal);
		color: var(--color-ink-secondary);
	}

	.note {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
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

	.act:hover:not(:disabled) {
		color: var(--color-ink);
		background: var(--color-lifted);
	}

	.act:disabled {
		cursor: default;
		opacity: 0.5;
	}

	.act:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}
</style>
