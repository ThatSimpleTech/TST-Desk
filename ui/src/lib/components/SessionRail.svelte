<script lang="ts">
	// Session rail (TD-1701): the collapsible session list at the window's
	// left edge — "the collapsible chat history on the left, like Claude."
	// Expanded it's a 260px column with a filter field, a New action, and the
	// workspace's sessions newest-first; collapsed it's a 48px icon strip of
	// state dots. All data comes from the sessions store; rows are keyed by
	// session id and the daemon's list is the only source of rows.
	import { onMount } from 'svelte';
	import Icon from './Icon.svelte';
	import { chat } from '../chat-store.svelte.js';
	import { workspaceName } from '../session-status.svelte.js';
	import {
		sessions,
		startSessions,
		setFilter,
		visibleRows,
		toggleCollapsed,
		selectRow,
		newSession,
		stateTone,
		rowTitle,
		rowSubtitle,
		ROW_STATE_LABELS,
		type SessionRow,
	} from '../sessions.svelte.js';

	onMount(() => startSessions());

	function activeTitle(row: SessionRow): string {
		return `${workspaceName(row.workspacePath)} · ${ROW_STATE_LABELS[row.state]}`;
	}
</script>

<aside class="rail" class:collapsed={sessions.collapsed} aria-label="Sessions">
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
		<div class="mini-list">
			{#each visibleRows() as row (row.sessionId)}
				<button
					class="mini"
					class:mini-active={row.sessionId === chat.sessionId}
					type="button"
					title={activeTitle(row)}
					aria-label={`Attach to session ${row.sessionId.slice(0, 8)} (${ROW_STATE_LABELS[row.state]})`}
					aria-current={row.sessionId === chat.sessionId ? 'true' : undefined}
					onclick={() => selectRow(row.sessionId)}
				>
					<span class="dot dot-{stateTone(row.state)}" aria-hidden="true"></span>
				</button>
			{/each}
		</div>
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
		<div class="list" role="list" aria-label="Sessions">
			{#each visibleRows() as row (row.sessionId)}
				<button
					class="row"
					class:row-active={row.sessionId === chat.sessionId}
					type="button"
					aria-current={row.sessionId === chat.sessionId ? 'true' : undefined}
					onclick={() => selectRow(row.sessionId)}
				>
					<span class="dot dot-{stateTone(row.state)}" aria-hidden="true"></span>
					<span class="row-text">
						<span class="row-title">{rowTitle(row)}</span>
						<span class="row-sub">{rowSubtitle(row)} · {ROW_STATE_LABELS[row.state]}</span>
					</span>
				</button>
			{:else}
				<p class="empty">
					{sessions.rows.length === 0 ? 'No sessions yet' : 'No matching sessions'}
				</p>
			{/each}
		</div>
	{/if}
</aside>

<style>
	.rail {
		flex: 0 0 260px;
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
		padding-top: var(--space-2);
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

	.row {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		text-align: left;
		border: none;
		border-left: 2px solid transparent;
		background: transparent;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		color: var(--color-ink);
	}

	.row:hover {
		background: var(--color-lifted);
	}

	.row-active,
	.row-active:hover {
		background: var(--color-lifted);
		border-left-color: var(--color-accent);
	}

	.row-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
		line-height: var(--leading-tight);
	}

	.row-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		font-family: var(--font-mono);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.row-sub {
		font-size: var(--text-xs);
		font-family: var(--font-sans);
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
</style>
