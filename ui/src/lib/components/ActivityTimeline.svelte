<script lang="ts">
	// Virtualized activity timeline (TD-1005).
	//
	// Renders the reactive `entries` list from timeline-store.ts through a
	// fixed-row window (virtualization.ts) so a thousand-entry session keeps
	// the DOM small (AC #7). Rows expand inline for full detail; expansion
	// changes one row's height, which the overscan buffer absorbs without
	// affecting the window math.
	import { entries } from '../timeline-store';
	import { computeWindow } from '../virtualization';
	import TimelineEntryRow from './TimelineEntryRow.svelte';

	// Collapsed row height — must match TimelineEntryRow.svelte's `.row`.
	const ROW_HEIGHT = 32;

	let scrollTop = $state(0);
	let viewportHeight = $state(0);
	let expandedId = $state<string | null>(null);

	let win = $derived(computeWindow(entries.length, ROW_HEIGHT, scrollTop, viewportHeight));

	function onScroll(e: Event): void {
		scrollTop = (e.currentTarget as HTMLElement).scrollTop;
	}

	function toggle(id: string): void {
		expandedId = expandedId === id ? null : id;
	}
</script>

<div class="timeline" bind:clientHeight={viewportHeight} onscroll={onScroll}>
	{#if entries.length === 0}
		<p class="empty">No activity yet.</p>
	{:else}
		<div class="spacer" style={`height: ${win.topPad}px`} aria-hidden="true"></div>
		{#each entries.slice(win.start, win.end) as entry (entry.id)}
			<TimelineEntryRow {entry} expanded={expandedId === entry.id} ontoggle={() => toggle(entry.id)} />
		{/each}
		<div class="spacer" style={`height: ${win.bottomPad}px`} aria-hidden="true"></div>
	{/if}
</div>

<style>
	.timeline {
		height: 100%;
		overflow-y: auto;
		overflow-x: hidden;
	}

	.empty {
		padding: var(--space-6);
		color: var(--color-text-muted);
		font-size: var(--text-sm);
		text-align: center;
	}

	.spacer {
		flex-shrink: 0;
	}
</style>
