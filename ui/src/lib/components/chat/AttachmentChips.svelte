<script lang="ts">
	// Attachment chips (TD-1709), shared by the composer and the sent row so
	// what the user staged and what the transcript shows are the same object
	// visually. `onremove` is what separates the two: present while the row is
	// still a draft, absent once it has gone out.
	import { formatBytes, type AttachmentChip } from "../../attachments";
	import Icon from "../Icon.svelte";

	let {
		chips,
		onremove,
		label = "Attachments",
	}: {
		chips: readonly AttachmentChip[];
		/** Draft-only: removing a file from a message already sent is a lie. */
		onremove?: (index: number) => void;
		label?: string;
	} = $props();
</script>

{#if chips.length > 0}
	<ul class="chips" aria-label={label}>
		{#each chips as chip, i (`${i}-${chip.name}`)}
			<li class="chip">
				<Icon name="file" size={12} />
				<span class="name" title={`${chip.name} — ${formatBytes(chip.size)}`}>{chip.name}</span>
				<span class="size">{formatBytes(chip.size)}</span>
				{#if onremove}
					<button
						type="button"
						class="remove"
						title="Remove {chip.name}"
						aria-label="Remove {chip.name}"
						onclick={() => onremove(i)}
					>
						<Icon name="x" size={12} />
					</button>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		max-width: 100%;
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
	}

	/* The name is the identity, so it takes the truncation rather than
	   letting a long file name push the size off the row. */
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--color-ink);
	}

	.size {
		flex-shrink: 0;
		font-family: var(--font-mono);
		color: var(--color-ink-muted);
	}

	.remove {
		display: inline-flex;
		align-items: center;
		flex-shrink: 0;
		padding: 0;
		color: var(--color-ink-muted);
		background: transparent;
		border: none;
		cursor: pointer;
	}

	.remove:hover {
		color: var(--color-ink);
	}

	.remove:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
		border-radius: var(--radius-sm);
	}
</style>
