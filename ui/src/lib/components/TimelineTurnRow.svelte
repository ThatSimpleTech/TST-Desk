<script lang="ts">
	// Turn header in the activity timeline: the user's message that opened
	// the turn, numbered, with what the daemon measured once it closed. The
	// rows underneath it belong to this turn until the next header.
	//
	// Since the navigation round it is also where a turn is worked from: a
	// chevron folds the turn's rows away (ActivityTimeline keeps the set),
	// and the label is a button that scrolls the chat to the message that
	// opened the turn, when the chat can find it.
	//
	// Exactly ROW_HEIGHT (32px) tall and never expandable, so the virtual
	// window in ActivityTimeline can treat it like any other collapsed row.
	// All wording and colour come from entry-view.ts; this stays a view.
	import Icon from './Icon.svelte';
	import type { TimelineEntry } from '../timeline';
	import { entryIcon, entryTone, turnMetrics } from '../entry-view';

	let {
		entry,
		collapsed = false,
		hidden = 0,
		ontoggle,
		onjump
	}: {
		entry: TimelineEntry;
		collapsed?: boolean;
		/** Rows the fold is hiding — shown on the header while collapsed. */
		hidden?: number;
		ontoggle?: () => void;
		/** Present when the chat holds this turn's message. */
		onjump?: () => void;
	} = $props();

	let tone = $derived(entryTone(entry));
	let icon = $derived(entryIcon(entry));
	let metrics = $derived(turnMetrics(entry));
	let live = $derived(entry.details.status === 'running');
	let errorCode = $derived(
		typeof entry.details.error_code === 'string' ? (entry.details.error_code as string) : null
	);
	// The tooltip carries what the row can't fit: the prompt's first line in
	// full, and the daemon's error code when the turn failed.
	let hint = $derived(errorCode ? `${entry.preview} · ${errorCode}` : entry.preview);
	let foldLabel = $derived(`${collapsed ? 'Expand' : 'Collapse'} ${entry.title.toLowerCase()}`);
</script>

<div class="turn turn--{tone}" class:turn--collapsed={collapsed} role="heading" aria-level={3} title={hint}>
	<button class="fold" type="button" aria-expanded={!collapsed} aria-label={foldLabel} onclick={ontoggle}>
		<span class="fold-icon" class:fold-icon--open={!collapsed} aria-hidden="true">
			<Icon name="chevron-right" size={12} />
		</span>
	</button>
	<span class="glyph" aria-hidden="true"><Icon name={icon} size={14} /></span>
	{#if onjump}
		<button class="jump" type="button" title={`${hint} · click to show in the chat`} onclick={onjump}>
			<span class="turn-label">{entry.title}</span>
			<span class="turn-prompt">{entry.preview}</span>
		</button>
	{:else}
		<span class="jump jump--static">
			<span class="turn-label">{entry.title}</span>
			<span class="turn-prompt">{entry.preview}</span>
		</span>
	{/if}
	{#if collapsed && hidden > 0}
		<span class="turn-hidden">{hidden} {hidden === 1 ? 'row' : 'rows'}</span>
	{/if}
	<span class="turn-metrics" class:turn-metrics--live={live}>{metrics}</span>
</div>

<style>
	.turn {
		/* Same height as TimelineEntryRow's collapsed `.row`: the virtual
		   window assumes every unexpanded row is ROW_HEIGHT. */
		height: 32px;
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: 0 var(--space-3) 0 var(--space-1);
		border-bottom: var(--border-width) solid var(--color-hairline);
		background: var(--color-sunken);
		color: var(--color-ink);
		font-size: var(--text-xs);
	}

	/* Same disclosure control as the rows and the Files pane, one size down. */
	.fold {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		width: 1.25rem;
		height: 1.25rem;
		padding: 0;
		border: none;
		border-radius: var(--radius-sm);
		background: none;
		color: var(--color-ink-muted);
		cursor: pointer;
	}

	.fold:hover {
		color: var(--color-ink);
		background: var(--color-lifted);
	}

	.fold-icon {
		display: inline-flex;
		transition: transform var(--dur-exit) var(--ease-out);
	}

	.fold-icon--open {
		transform: rotate(90deg);
	}

	.glyph {
		display: inline-flex;
		flex-shrink: 0;
		color: var(--color-ink-muted);
	}

	.turn--warning .glyph { color: var(--color-warn); }
	.turn--danger .glyph { color: var(--color-err); }

	/* The label and prompt share one button so the whole readable stretch
	   of the header is the click target, not a word of it. */
	.jump {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: 0;
		border: none;
		background: none;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}

	.jump--static {
		cursor: default;
	}

	.jump:not(.jump--static):hover .turn-label {
		color: var(--color-accent);
	}

	/* Same treatment as the kind label on the rows below, one step darker,
	   so the header reads as the heading of that group rather than a row. */
	.turn-label {
		flex-shrink: 0;
		font-weight: var(--weight-semibold);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.turn-prompt {
		flex: 1;
		min-width: 0;
		color: var(--color-ink-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* What a folded turn is sitting on. */
	.turn-hidden {
		flex-shrink: 0;
		padding: 0 var(--space-2);
		border-radius: var(--radius-full);
		background: color-mix(in srgb, var(--color-ink) 8%, transparent);
		color: var(--color-ink-muted);
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.turn-metrics {
		flex-shrink: 0;
		color: var(--color-ink-muted);
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	/* A turn still running reads a shade stronger: it is the live one. */
	.turn-metrics--live {
		color: var(--color-ink-secondary);
	}

	.turn--warning .turn-metrics { color: var(--color-warn); }
	.turn--danger .turn-metrics { color: var(--color-err); }
</style>
