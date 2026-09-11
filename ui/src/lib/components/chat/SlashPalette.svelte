<script lang="ts">
	// Slash-command picker (TD-4501). Presentational — the list comes from
	// the last command_list the daemon sent. Click / Enter inserts; a
	// secondary Send action ships immediately.
	import type { CommandEntry } from "../../protocol";

	let {
		items,
		selectedIndex = 0,
		oninsert,
		onsend,
		onhover,
	}: {
		items: CommandEntry[];
		selectedIndex?: number;
		oninsert: (command: CommandEntry) => void;
		onsend: (command: CommandEntry) => void;
		onhover?: (index: number) => void;
	} = $props();

	const optionId = (index: number): string => `slash-option-${index}`;
</script>

<div
	class="palette"
	id="slash-list"
	role="listbox"
	aria-label="Slash commands"
	tabindex="-1"
>
	{#if items.length === 0}
		<p class="empty">No slash commands</p>
	{:else}
		{#each items as command, i (command.name)}
			<div
				class="row"
				class:row--active={i === selectedIndex}
				id={optionId(i)}
				role="option"
				aria-selected={i === selectedIndex}
				aria-disabled={command.too_large === true}
			>
				<button
					class="pick"
					type="button"
					disabled={command.too_large === true}
					onclick={() => oninsert(command)}
					onmouseenter={() => onhover?.(i)}
				>
					<span class="name">/{command.name}</span>
					<span class="sub">
						{#if command.too_large}
							too large to insert
						{:else if command.description}
							{command.description}
						{:else}
							{command.source}
						{/if}
					</span>
				</button>
				<button
					class="send"
					type="button"
					disabled={command.too_large === true}
					title="Insert and send"
					onclick={() => onsend(command)}
				>
					Send
				</button>
			</div>
		{/each}
	{/if}
</div>

<style>
	.palette {
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 14rem;
		overflow-y: auto;
		padding: var(--space-2);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-sm);
	}

	.row {
		display: flex;
		align-items: stretch;
		gap: var(--space-1);
		border-radius: var(--radius-md);
	}

	.row--active,
	.row--active:hover {
		background: var(--color-sunken);
	}

	.pick {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 2px;
		padding: var(--space-2) var(--space-3);
		text-align: left;
		border: none;
		border-radius: var(--radius-md);
		background: transparent;
		color: var(--color-ink);
		cursor: pointer;
	}

	.pick:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.sub {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.send {
		flex-shrink: 0;
		align-self: center;
		margin-right: var(--space-2);
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		cursor: pointer;
	}

	.send:hover:not(:disabled) {
		color: var(--color-ink);
		border-color: var(--color-ink-muted);
	}

	.send:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.empty {
		margin: var(--space-3) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		text-align: center;
	}
</style>
