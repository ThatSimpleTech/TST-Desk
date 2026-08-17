<script lang="ts">
	// Command palette (TD-1707): ⌘K over the sessions and the actions.
	//
	// Presentational — the entry list, the matching, and the dispatch all live
	// in the store (palette-store.svelte.ts) so they are testable without
	// mounting anything. Escape is deliberately *not* handled here: the shell's
	// one keydown handler owns the layer order (shortcuts.ts), and a second
	// listener would be a second opinion about what Escape means.
	import Icon from './Icon.svelte';
	import {
		palette,
		visibleEntries,
		setPaletteQuery,
		movePaletteSelection,
		runPaletteSelection,
		runPaletteEntry
	} from '../palette-store.svelte.js';

	let field = $state<HTMLInputElement | null>(null);
	let entries = $derived(visibleEntries());

	const optionId = (index: number): string => `palette-option-${index}`;

	// A keyboard affordance is useless until the caret is in it.
	$effect(() => {
		if (palette.open) field?.focus();
	});

	// Keep the highlight visible when the list is longer than the box.
	$effect(() => {
		if (!palette.open) return;
		document.getElementById(optionId(palette.index))?.scrollIntoView({ block: 'nearest' });
	});

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'ArrowDown') {
			event.preventDefault();
			movePaletteSelection(1);
		} else if (event.key === 'ArrowUp') {
			event.preventDefault();
			movePaletteSelection(-1);
		} else if (event.key === 'Enter') {
			event.preventDefault();
			runPaletteSelection();
		}
	}
</script>

{#if palette.open}
	<div class="overlay">
		<div class="palette" role="dialog" aria-modal="true" aria-label="Command palette">
			<div class="field">
				<span class="field-icon" aria-hidden="true"><Icon name="search" size={15} /></span>
				<input
					bind:this={field}
					class="input"
					type="text"
					role="combobox"
					aria-expanded="true"
					aria-controls="palette-list"
					aria-activedescendant={entries.length > 0 ? optionId(palette.index) : undefined}
					aria-label="Search sessions and actions"
					placeholder="Search sessions and actions…"
					autocomplete="off"
					spellcheck="false"
					value={palette.query}
					oninput={(e) => setPaletteQuery(e.currentTarget.value)}
					onkeydown={onKeydown}
				/>
			</div>

			<div class="list" id="palette-list" role="listbox" aria-label="Results" tabindex="-1">
				{#each entries as entry, i (entry.id)}
					<button
						class="entry"
						class:entry--active={i === palette.index}
						id={optionId(i)}
						type="button"
						role="option"
						aria-selected={i === palette.index}
						onclick={() => runPaletteEntry(entry)}
					>
						<span class="entry-icon" aria-hidden="true"><Icon name={entry.icon} size={15} /></span>
						<span class="entry-text">
							<span class="entry-title">{entry.title}</span>
							<span class="entry-sub">{entry.subtitle}</span>
						</span>
					</button>
				{:else}
					<p class="empty">No matches</p>
				{/each}
			</div>
		</div>
	</div>
{/if}

<style>
	.overlay {
		position: fixed;
		inset: 0;
		z-index: 50;
		display: flex;
		justify-content: center;
		/* Sits high, the way a palette is expected to — not centered. */
		padding-top: 12vh;
		background: rgb(0 0 0 / 0.28);
	}

	.palette {
		display: flex;
		flex-direction: column;
		width: min(34rem, 92vw);
		max-height: 60vh;
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-xl);
		box-shadow: var(--shadow-lg);
		overflow: hidden;
	}

	.field {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-3) var(--space-4);
		border-bottom: var(--border-width) solid var(--color-hairline);
		color: var(--color-ink-muted);
		flex-shrink: 0;
	}

	.field-icon {
		display: inline-flex;
		line-height: 1;
	}

	.input {
		flex: 1;
		min-width: 0;
		border: none;
		background: transparent;
		font-family: var(--font-sans);
		font-size: var(--text-base);
		color: var(--color-ink);
		outline: none;
	}

	.input::placeholder {
		color: var(--color-ink-muted);
	}

	.list {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: var(--space-2);
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.entry {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		text-align: left;
		border: none;
		border-radius: var(--radius-md);
		background: transparent;
		color: var(--color-ink);
		padding: var(--space-2) var(--space-3);
		cursor: pointer;
	}

	.entry:hover {
		background: var(--color-sunken);
	}

	/* The keyboard highlight has to read louder than hover — it is the thing
	   Enter acts on, and the pointer may be sitting anywhere. */
	.entry--active,
	.entry--active:hover {
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.entry-icon {
		display: inline-flex;
		line-height: 1;
		color: var(--color-ink-secondary);
	}

	.entry--active .entry-icon,
	.entry--active .entry-sub {
		color: var(--color-on-accent);
	}

	.entry-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
		line-height: var(--leading-tight);
	}

	.entry-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.entry-sub {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.empty {
		margin: var(--space-4) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		text-align: center;
	}
</style>
