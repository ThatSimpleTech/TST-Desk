<script lang="ts">
	// Virtualized activity timeline (TD-1005).
	//
	// Renders the reactive `entries` list from timeline-store.ts through a
	// fixed-row window (virtualization.ts) so a thousand-entry session keeps
	// the DOM small (AC #7). Rows expand inline for full detail; expansion
	// changes one row's height, which the overscan buffer absorbs without
	// affecting the window math.
	//
	// Turn headers are entries too (kind `turn`, one per user message), so
	// grouping costs the window nothing: a header is just a 32px row that
	// happens not to expand.
	//
	// Since the navigation round the window runs over a folded view of the
	// list (timeline-view.ts): a chip row narrows it to tools, approvals or
	// errors, and any turn can be collapsed to its header. The rules live
	// in timeline-view.ts; this holds the chip and the set of folded ids.
	import { SvelteSet } from 'svelte/reactivity';
	import { entries } from '../timeline-store.svelte.js';
	import { computeWindow } from '../virtualization';
	import { jumpToTurn } from '../chat-jump.svelte.js';
	import {
		TIMELINE_FILTERS,
		filterCounts,
		foldTimeline,
		turnIdOf,
		turnIds,
		type TimelineFilter
	} from '../timeline-view';
	import EmptyState from './EmptyState.svelte';
	import TimelineEntryRow from './TimelineEntryRow.svelte';
	import TimelineTurnRow from './TimelineTurnRow.svelte';

	// Collapsed row height — must match `.row` in TimelineEntryRow.svelte and
	// `.turn` in TimelineTurnRow.svelte.
	const ROW_HEIGHT = 32;

	let scrollEl = $state<HTMLDivElement | null>(null);
	let scrollTop = $state(0);
	let viewportHeight = $state(0);
	let expandedId = $state<string | null>(null);
	let filter = $state<TimelineFilter>('all');
	// Header ids the user folded shut. A SvelteSet so `has` is tracked.
	const collapsed = new SvelteSet<string>();

	let counts = $derived(filterCounts(entries));
	let view = $derived(foldTimeline(entries, filter, collapsed));
	let headers = $derived(turnIds(entries));
	let anyCollapsed = $derived(headers.some((id) => collapsed.has(id)));
	let win = $derived(computeWindow(view.rows.length, ROW_HEIGHT, scrollTop, viewportHeight));
	let activeChip = $derived(TIMELINE_FILTERS.find((c) => c.id === filter) ?? TIMELINE_FILTERS[0]);

	function onScroll(e: Event): void {
		scrollTop = (e.currentTarget as HTMLElement).scrollTop;
	}

	function toggle(id: string): void {
		expandedId = expandedId === id ? null : id;
	}

	function toggleTurn(id: string): void {
		if (collapsed.has(id)) collapsed.delete(id);
		else collapsed.add(id);
	}

	function foldAll(): void {
		if (anyCollapsed) {
			collapsed.clear();
		} else {
			for (const id of headers) collapsed.add(id);
		}
	}

	// A new filter is a new list; start it from the top rather than from
	// wherever the old one was scrolled to.
	function pick(next: TimelineFilter): void {
		filter = next;
		if (scrollEl) scrollEl.scrollTop = 0;
	}
</script>

<div class="pane">
	{#if entries.length === 0}
		<EmptyState
			icon="clock"
			title="No activity yet"
			body="Tool calls, approvals and decisions land here as the agent works, grouped by turn with what each one took and cost."
		/>
	{:else}
		<div class="bar">
			<div class="chips" role="group" aria-label="Filter activity">
				{#each TIMELINE_FILTERS as chip (chip.id)}
					<button
						class="chip"
						class:chip--on={filter === chip.id}
						type="button"
						aria-pressed={filter === chip.id}
						title={chip.hint}
						onclick={() => pick(chip.id)}
					>
						{chip.label}
						{#if chip.id !== 'all'}
							<span class="chip-count">{counts[chip.id]}</span>
						{/if}
					</button>
				{/each}
			</div>
			{#if headers.length > 1}
				<button class="fold-all" type="button" onclick={foldAll}>
					{anyCollapsed ? 'Expand all' : 'Collapse all'}
				</button>
			{/if}
		</div>
		<div class="timeline" bind:this={scrollEl} bind:clientHeight={viewportHeight} onscroll={onScroll}>
			{#if view.rows.length === 0}
				<EmptyState compact body={`No ${activeChip.label.toLowerCase()} in this session yet.`} />
			{:else}
				<div class="spacer" style={`height: ${win.topPad}px`} aria-hidden="true"></div>
				{#each view.rows.slice(win.start, win.end) as entry (entry.id)}
					{#if entry.kind === 'turn'}
						{@const fold = view.turns.get(entry.id)}
						{@const turnId = turnIdOf(entry)}
						<TimelineTurnRow
							{entry}
							collapsed={collapsed.has(entry.id)}
							hidden={fold?.hidden ?? 0}
							ontoggle={() => toggleTurn(entry.id)}
							onjump={turnId ? () => jumpToTurn(turnId) : undefined}
						/>
					{:else}
						<TimelineEntryRow
							{entry}
							expanded={expandedId === entry.id}
							ontoggle={() => toggle(entry.id)}
						/>
					{/if}
				{/each}
				<div class="spacer" style={`height: ${win.bottomPad}px`} aria-hidden="true"></div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.pane {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
	}

	/* The chip row sits outside the scroll box, so the window math sees
	   rows from the top of the box and nothing else. */
	.bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
		flex-shrink: 0;
		padding: var(--space-2) var(--space-3);
		border-bottom: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
	}

	.chips {
		display: flex;
		gap: var(--space-1);
		min-width: 0;
		overflow-x: auto;
	}

	/* Same pill as the Usage pane's period picker. */
	.chip {
		display: inline-flex;
		align-items: baseline;
		gap: var(--space-1);
		flex-shrink: 0;
		padding: 2px var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		background: transparent;
		color: var(--color-ink-secondary);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		cursor: pointer;
		transition: border-color var(--transition-fast), color var(--transition-fast);
	}

	.chip:hover {
		border-color: var(--color-accent);
		color: var(--color-ink);
	}

	.chip--on,
	.chip--on:hover {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.chip-count {
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		opacity: 0.8;
	}

	.fold-all {
		flex-shrink: 0;
		padding: 2px var(--space-1);
		border: none;
		border-radius: var(--radius-sm);
		background: none;
		color: var(--color-ink-muted);
		font-size: var(--text-xs);
		cursor: pointer;
	}

	.fold-all:hover {
		color: var(--color-ink);
		background: var(--color-sunken);
	}

	.timeline {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		overflow-x: hidden;
	}

	.spacer {
		flex-shrink: 0;
	}
</style>
