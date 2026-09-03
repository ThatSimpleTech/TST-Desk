<script lang="ts">
	// Session rail (TD-1701): the collapsible session list at the window's
	// left edge — "the collapsible chat history on the left, like Claude."
	// Expanded it's a 260px column with a filter field, a New action, and the
	// workspace's sessions newest-first; collapsed it's a 48px icon strip of
	// state dots. All data comes from the sessions store; rows are keyed by
	// session id and the daemon's list is the only source of rows.
	//
	// TD-1712 gave it its information architecture: function surfaces grouped
	// above, session history sectioned below with a count badge, and the
	// account/settings row anchored bottom-left. The grouping rules are pure
	// (../rail.ts) — this file only renders them.
	//
	// TD-1715 added the row lifecycle affordances and the archived shelf. The
	// rows themselves moved to RailSessionRow, which owns a row's menu/confirm
	// states; this file stayed the list.
	import { onMount } from 'svelte';
	import Icon from './Icon.svelte';
	import RailFunctions from './RailFunctions.svelte';
	import RailAccount from './RailAccount.svelte';
	import RailSessionRow from './RailSessionRow.svelte';
	import { chat } from '../chat-store.svelte.js';
	import { archivedToggle, emptyRowsCopy, railSections, starredToggle } from '../rail';
	import { toggleArchivedView, toggleStarredOnly } from '../session-actions.svelte.js';
	import {
		sessions,
		startSessions,
		setFilter,
		visibleRows,
		shelfRowCount,
		toggleCollapsed,
		selectRow,
		newSession,
		activityTone,
		ACTIVITY_LABELS,
		liveActivity,
		rowTitleFull,
		type SessionRow,
	} from '../sessions.svelte.js';
	import { DIVIDER_HIT_MIN_PX, attachDragListeners } from '../splitpane';
	import {
		DEFAULT_RAIL_PX,
		railMaxPx,
		MIN_RAIL_PX,
		RAIL_KEYBOARD_STEP_PX,
		clampRailPx,
		readPersistedRailPx,
		writePersistedRailPx
	} from '../rail-width';

	onMount(() => startSessions());

	// TD-4826: the rail's width is user-adjustable, so the conversation pane can
	// be widened from its left edge too (the chat|activity divider already
	// covers the right). The divider mirrors SplitPane's: window listeners are
	// the mechanism, pointer capture only an optimisation (TD-1011).
	let railPx = $state(DEFAULT_RAIL_PX);
	let dragging = $state(false);

	let railRoot: HTMLElement | undefined = $state();
	let detachDrag: (() => void) | null = null;

	function layoutMax(): number {
		if (typeof window === 'undefined') return MIN_RAIL_PX;
		return railMaxPx(window.innerWidth);
	}

	function persist() {
		writePersistedRailPx(window.localStorage, railPx);
	}

	function onDividerDown(e: PointerEvent) {
		if (dragging) return;
		dragging = true;
		(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
		detachDrag = attachDragListeners(window, onDividerMove, onDividerUp);
	}

	function onDividerMove(e: PointerEvent) {
		if (!dragging) return;
		const next = clampRailPx(
			e.clientX - (railRoot?.getBoundingClientRect().left ?? 0),
			layoutMax()
		);
		railPx = next;
	}

	function onDividerUp() {
		if (!dragging) return;
		dragging = false;
		detachDrag?.();
		detachDrag = null;
		// Persist once the drag settles.
		persist();
	}

	function onDividerKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowLeft') {
			railPx = clampRailPx(railPx - RAIL_KEYBOARD_STEP_PX, layoutMax());
		} else if (e.key === 'ArrowRight') {
			railPx = clampRailPx(railPx + RAIL_KEYBOARD_STEP_PX, layoutMax());
		} else {
			return;
		}
		e.preventDefault();
		persist();
	}

	// Restore the persisted width on mount; a drag still in flight when the
	// rail unmounts must not leave window listeners behind.
	$effect(() => {
		railPx = clampRailPx(readPersistedRailPx(window.localStorage), layoutMax());
	});
	$effect(() => {
		return () => {
			detachDrag?.();
			detachDrag = null;
		};
	});

	function activeTitle(row: SessionRow): string {
		const activity = liveActivity(row);
		return `${rowTitleFull(row)} · ${ACTIVITY_LABELS[activity]}`;
	}

	let rows = $derived(visibleRows());
	// The history section badges what it actually lists, so the count can
	// never disagree with the rows under it — and its heading names the shelf
	// you are on, which is the only thing distinguishing the two.
	let history = $derived(railSections(rows.length, sessions.showArchived)[1]);
	let shelfToggle = $derived(archivedToggle(sessions.showArchived));
	let starToggle = $derived(starredToggle(sessions.showStarredOnly));
	let emptyCopy = $derived(
		emptyRowsCopy(
			sessions.showArchived,
			sessions.filter.trim() !== '',
			shelfRowCount() > 0,
			sessions.showStarredOnly
		)
	);
</script>

<aside
	class="rail"
	class:collapsed={sessions.collapsed}
	class:dragging
	aria-label="Sessions"
	bind:this={railRoot}
	style={!sessions.collapsed ? `flex-basis: ${railPx}px` : undefined}
>
	{#if sessions.collapsed}
		<!-- Icon strip: expand, New, then one dot per session. -->
		<button
			class="icon-btn"
			type="button"
			title="Show sessions"
			aria-label="Show sessions"
			onclick={toggleCollapsed}><Icon name="panel-left" size={16} /></button
		>
		<button
			class="icon-btn"
			type="button"
			title="New session"
			aria-label="New session"
			disabled={chat.sessionId === null && sessions.rows.length === 0}
			onclick={() => newSession()}><Icon name="plus" size={16} /></button
		>
		<RailFunctions compact />
		<div class="mini-list">
			{#each rows as row (row.sessionId)}
				<button
					class="mini"
					class:mini-active={row.sessionId === chat.sessionId}
					type="button"
					title={activeTitle(row)}
					aria-label={`Attach to session ${rowTitleFull(row)} (${ACTIVITY_LABELS[liveActivity(row)]})`}
					aria-current={row.sessionId === chat.sessionId ? 'true' : undefined}
					onclick={() => selectRow(row.sessionId)}
				>
					<span
						class="dot dot-{activityTone(liveActivity(row))}"
						class:dot-live={liveActivity(row) === 'working'}
						aria-hidden="true"
					></span>
				</button>
			{/each}
		</div>
		<RailAccount compact />
	{:else}
		<div class="head">
			<div class="filter">
				<span class="filter-icon" aria-hidden="true"><Icon name="search" size={13} /></span>
				<input
					type="text"
					placeholder="Filter sessions"
					aria-label="Filter sessions"
					value={sessions.filter}
					oninput={(e) => setFilter(e.currentTarget.value)}
				/>
			</div>
			<button
				class="icon-btn"
				type="button"
				title="New session"
				aria-label="New session"
				disabled={chat.sessionId === null && sessions.rows.length === 0}
				onclick={() => newSession()}><Icon name="plus" size={16} /></button
			>
			<button
				class="icon-btn"
				type="button"
				title="Hide sessions"
				aria-label="Hide sessions"
				onclick={toggleCollapsed}><Icon name="panel-left" size={16} /></button
			>
		</div>
		<RailFunctions />
		<div class="section-head">
			<span class="section-label">{history.label}</span>
			{#if history.badge !== null}
				<span class="badge">{history.badge}</span>
			{/if}
			<div class="shelf-group">
				<button
					class="shelf"
					type="button"
					title={starToggle.hint}
					aria-label={starToggle.hint}
					aria-pressed={sessions.showStarredOnly}
					onclick={toggleStarredOnly}
				>
					<Icon name="star" size={12} />
					<span>{starToggle.label}</span>
				</button>
				<button
					class="shelf"
					type="button"
					title={shelfToggle.hint}
					aria-label={shelfToggle.hint}
					aria-pressed={sessions.showArchived}
					onclick={toggleArchivedView}
				>
					<Icon name="archive" size={12} />
					<span>{shelfToggle.label}</span>
				</button>
			</div>
		</div>
		<div class="list" role="list" aria-label={history.label}>
			{#each rows as row (row.sessionId)}
				<RailSessionRow
					{row}
					active={row.sessionId === chat.sessionId}
					onselect={() => selectRow(row.sessionId)}
				/>
			{:else}
				<p class="empty">{emptyCopy}</p>
			{/each}
		</div>
		<RailAccount />
	{/if}
	<!-- Width divider (TD-4826): same ARIA separator pattern as SplitPane. -->
	{#if !sessions.collapsed}
		<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
		<div
			class="resize-handle"
			role="separator"
			tabindex="0"
			aria-orientation="vertical"
			aria-label="Session rail width"
			aria-valuenow={railPx}
			aria-valuemin={MIN_RAIL_PX}
			aria-valuemax={layoutMax()}
			style={`--divider-hit: ${DIVIDER_HIT_MIN_PX}px;`}
			onpointerdown={onDividerDown}
			onkeydown={onDividerKeydown}
		></div>
	{/if}
</aside>

<style>
	.rail {
		flex: 0 0 260px;
		position: relative;
		display: flex;
		flex-direction: column;
		min-height: 0;
		background: var(--color-sunken);
		border-right: var(--border-width) solid var(--color-hairline);
		transition: flex-basis var(--transition-base);
	}

	.rail.collapsed {
		flex-basis: 48px;
		align-items: center;
		gap: var(--space-1);
		padding: var(--space-2) 0;
	}

	/* A drag must move 1:1 with the pointer — no basis transition mid-drag. */
	.rail.dragging {
		transition: none;
	}

	/* Width divider (TD-4826): invisible until hovered/focused, centered on
	   the rail's right border; hit target at least DIVIDER_HIT_MIN_PX without
	   widening the painted rule (mirrors SplitPane, TD-1011). */
	.resize-handle {
		position: absolute;
		top: 0;
		bottom: 0;
		right: calc(var(--divider-hit) / -2);
		width: var(--divider-hit);
		cursor: col-resize;
		touch-action: none;
		z-index: 1;
	}

	.resize-handle::after {
		content: "";
		position: absolute;
		top: 0;
		bottom: 0;
		left: 50%;
		width: var(--border-width);
		transform: translateX(-50%);
		background: transparent;
		transition: background var(--transition-fast);
	}

	.resize-handle:hover::after,
	.resize-handle:focus-visible::after,
	.rail.dragging .resize-handle::after {
		background: var(--color-accent);
	}

	/* ── Section heading + count badge (TD-1712) ───────────────────── */

	.section-head {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-2) var(--space-1);
		margin: 0 var(--space-2);
		border-top: var(--border-width) solid var(--color-hairline);
		flex-shrink: 0;
	}

	.section-label {
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		/* Matches the timeline's uppercase kind label. */
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--color-ink-muted);
	}

	.badge {
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: 0 var(--space-2);
		line-height: var(--leading-relaxed);
	}

	/* Shelf toggle (TD-1715): the heading says where you are, this says where
	   the click goes. Pushed right so it never crowds the count. */
	.shelf-group {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		margin-left: auto;
	}

	.shelf {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		border: none;
		background: transparent;
		border-radius: var(--radius-sm);
		padding: 0 var(--space-1);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		cursor: pointer;
		line-height: var(--leading-relaxed);
	}

	.shelf:hover,
	.shelf[aria-pressed='true'] {
		color: var(--color-ink);
		background: var(--color-lifted);
	}

	/* ── Header (expanded) ─────────────────────────────────────────── */

	.head {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		padding: var(--space-2) var(--space-2) var(--space-1);
	}

	.filter {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: var(--space-1);
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: 0 var(--space-2);
		color: var(--color-ink-muted);
	}

	.filter:focus-within {
		border-color: var(--color-ink-muted);
	}

	.filter input {
		flex: 1;
		min-width: 0;
		border: none;
		background: transparent;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink);
		padding: var(--space-1) 0;
		outline: none;
	}

	.filter input::placeholder {
		color: var(--color-ink-muted);
	}

	.filter-icon {
		display: inline-flex;
		line-height: 1;
	}

	/* ── Icon buttons (both modes) ─────────────────────────────────── */

	.icon-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		background: transparent;
		color: var(--color-ink-secondary);
		cursor: pointer;
		padding: var(--space-1);
		border-radius: var(--radius-sm);
		line-height: 1;
	}

	.icon-btn:hover:not(:disabled) {
		background: var(--color-lifted);
		color: var(--color-ink);
	}

	.icon-btn:disabled {
		color: var(--color-ink-muted);
		cursor: default;
		opacity: 0.6;
	}

	/* ── Session rows (expanded) ───────────────────────────────────── */

	.list {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: var(--space-1) var(--space-2) var(--space-2);
		display: flex;
		flex-direction: column;
	}

	.empty {
		margin: var(--space-4) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		text-align: center;
	}

	/* ── Icon strip (collapsed) ────────────────────────────────────── */

	.mini-list {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-1);
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		width: 100%;
		padding-top: var(--space-1);
	}

	.mini {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border: 2px solid transparent;
		border-radius: var(--radius-full);
		background: transparent;
		cursor: pointer;
	}

	.mini:hover {
		background: var(--color-lifted);
	}

	.mini-active {
		border-color: var(--color-accent);
		background: var(--color-lifted);
	}

	/* ── State dot (both modes) ────────────────────────────────────── */

	.dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		flex-shrink: 0;
	}

	.dot-info {
		background: var(--color-accent);
	}
	.dot-warning {
		background: var(--color-warn);
	}
	.dot-danger {
		background: var(--color-err);
	}
	.dot-success {
		background: var(--color-ok);
	}
	.dot-muted {
		background: var(--color-ink-muted);
	}

	.dot-live {
		animation: dot-pulse 1.4s ease-in-out infinite;
	}

	@keyframes dot-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.4;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.dot-live {
			animation: none;
		}
	}
</style>
