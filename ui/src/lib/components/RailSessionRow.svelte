<script lang="ts">
	// One session row and its lifecycle affordances (TD-1715).
	//
	// Split out of SessionRail so the rail stays a list and this file owns the
	// row's three states: at rest (attach on click), menu open (Archive, Move,
	// Delete), and one of the two committed steps — Delete's confirm or Move's
	// project picker. Only one row can be in a non-rest state at a time; the
	// store enforces that, not this markup.
	//
	// Nothing here decides whether Delete or Move will be *allowed*. The daemon
	// alone knows if a turn is in flight, so both are always offered and its
	// refusal renders in place, under the row that asked.
	import Icon from './Icon.svelte';
	import { DELETE_CONFIRM, rowActions, type RailRowActionId } from '../rail';
	import { workspaceName } from '../session-status.svelte.js';
	import {
		closeRowMenus,
		ACTIVITY_LABELS,
		liveActivity,
		ROW_STATE_LABELS,
		rowSubtitle,
		rowTitle,
		rowTitleFull,
		sessions,
		activityTone,
		type SessionRow
	} from '../sessions.svelte.js';
	import {
		cancelRename,
		confirmDelete,
		moveRow,
		moveTargets,
		openSessionInNewWindow,
		renameSession,
		requestDelete,
		requestMove,
		requestRename,
		setArchived,
		setStarred,
		toggleRowMenu
	} from '../session-actions.svelte.js';

	let { row, active, onselect }: { row: SessionRow; active: boolean; onselect: () => void } =
		$props();

	let menuOpen = $derived(sessions.menuFor === row.sessionId);
	let confirming = $derived(sessions.confirmDeleteFor === row.sessionId);
	let moving = $derived(sessions.moveFor === row.sessionId);
	let renaming = $derived(sessions.renameFor === row.sessionId);
	let activity = $derived(liveActivity(row));
	let targets = $derived(moving ? moveTargets(row.sessionId) : []);
	let draft = $state('');
	let inputEl = $state<HTMLInputElement | undefined>(undefined);
	let renameSeed = $state<string | null>(null);

	// A titled row reads as a sentence, so it is set in the sans; only the
	// bare id fallback keeps the mono. The id itself lives in the tooltip.
	let untitled = $derived(row.title === null || row.title.trim() === '');
	// Idle and complete are the norm and the dot already says so; the
	// subtitle only spells out a state worth a second look.
	const QUIET_STATES: ReadonlySet<string> = new Set(['none', 'idle', 'complete']);
	let stateNote = $derived(QUIET_STATES.has(row.state) ? null : ROW_STATE_LABELS[row.state]);
	let tooltip = $derived(
		`${rowTitle(row)}\n${row.sessionId.slice(0, 8)} · ${rowSubtitle(row)} · ${ROW_STATE_LABELS[row.state]}`
	);

	$effect(() => {
		if (!renaming) {
			renameSeed = null;
			return;
		}
		if (renameSeed === row.sessionId) return;
		draft = rowTitleFull(row);
		renameSeed = row.sessionId;
	});

	$effect(() => {
		if (renaming && inputEl !== undefined) {
			inputEl.focus();
			inputEl.select();
		}
	});

	function run(id: RailRowActionId): void {
		if (id === 'star') setStarred(row.sessionId, true);
		else if (id === 'unstar') setStarred(row.sessionId, false);
		else if (id === 'rename') requestRename(row.sessionId);
		else if (id === 'archive') setArchived(row.sessionId, true);
		else if (id === 'unarchive') setArchived(row.sessionId, false);
		else if (id === 'move') requestMove(row.sessionId);
		else if (id === 'open-window') openSessionInNewWindow(row.sessionId);
		else if (id === 'delete') requestDelete(row.sessionId);
	}

	function commitRename(): void {
		if (sessions.renameFor !== row.sessionId) return;
		renameSession(row.sessionId, draft);
	}

	function onRenameKey(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			commitRename();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancelRename();
		}
	}
</script>

<div class="wrap" class:wrap-open={menuOpen || confirming || moving || renaming}>
	<div class="row" class:row-active={active}>
		<button
			class="open"
			type="button"
			data-rail-row={row.sessionId}
			aria-current={active ? 'true' : undefined}
			title={tooltip}
			onclick={onselect}
		>
			<span
				class="dot dot-{activityTone(activity)}"
				class:dot-live={activity === 'working'}
				aria-hidden="true"
			></span>
			<span class="row-text">
				<span class="row-title" class:row-title--id={untitled} title={rowTitleFull(row)}>{rowTitle(row)}</span>
				<span class="row-sub">
					{rowSubtitle(row)} · {ACTIVITY_LABELS[activity]}{#if stateNote !== null} · <span class="row-state">{stateNote}</span>{/if}
				</span>
			</span>
		</button>
		<button
			class="more"
			class:more-open={menuOpen}
			type="button"
			title="Session actions"
			aria-label={`Actions for session ${rowTitleFull(row)}`}
			aria-expanded={menuOpen}
			onclick={() => toggleRowMenu(row.sessionId)}><Icon name="ellipsis" size={14} /></button
		>
	</div>

	{#if menuOpen}
		<div class="menu" role="group" aria-label={`Actions for session ${rowTitleFull(row)}`}>
			{#each rowActions(row.archived, row.starred) as action (action.id)}
				<button
					class="action"
					class:action-danger={action.danger}
					type="button"
					title={action.hint}
					onclick={() => run(action.id)}
				>
					<Icon name={action.icon} size={14} />
					<span>{action.label}</span>
				</button>
			{/each}
		</div>
	{/if}

	{#if confirming}
		<div class="commit" role="alertdialog" aria-label="Confirm delete">
			<p class="commit-copy">{DELETE_CONFIRM}</p>
			<div class="commit-btns">
				<button class="btn btn-danger" type="button" onclick={() => confirmDelete()}>
					Delete
				</button>
				<button class="btn" type="button" onclick={closeRowMenus}>Cancel</button>
			</div>
		</div>
	{/if}

	{#if renaming}
		<div class="commit" role="group" aria-label="Rename session">
			<input
				class="rename"
				bind:this={inputEl}
				bind:value={draft}
				aria-label="Session name"
				onkeydown={onRenameKey}
				onblur={commitRename}
			/>
		</div>
	{/if}

	{#if moving}
		<div class="commit" role="group" aria-label="Move to project">
			{#each targets as target (target)}
				<button
					class="target"
					type="button"
					title={target}
					onclick={() => moveRow(row.sessionId, target)}
				>
					<Icon name="folder" size={13} />
					<span class="target-name">{workspaceName(target)}</span>
				</button>
			{:else}
				<p class="commit-copy">No other project is open yet. Open one first, then move.</p>
			{/each}
			<div class="commit-btns">
				<button class="btn" type="button" onclick={closeRowMenus}>Cancel</button>
			</div>
		</div>
	{/if}

	{#if sessions.refusal !== null && (confirming || moving || menuOpen)}
		<p class="refusal" role="status">{sessions.refusal}</p>
	{/if}
</div>

<style>
	.wrap {
		display: flex;
		flex-direction: column;
		border-radius: var(--radius-sm);
	}

	.wrap-open {
		background: var(--color-lifted);
	}

	.row {
		display: flex;
		align-items: center;
		border-left: 2px solid transparent;
		border-radius: var(--radius-sm);
	}

	.row:hover {
		background: var(--color-lifted);
	}

	.row-active,
	.row-active:hover {
		background: var(--color-lifted);
		border-left-color: var(--color-accent);
	}

	.open {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		flex: 1;
		min-width: 0;
		text-align: left;
		border: none;
		background: transparent;
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		color: var(--color-ink);
		border-radius: var(--radius-sm);
	}

	/* The ring sits inside the row: the list clips, and a ring drawn outside
	   a 260px column is a ring drawn over the next row. */
	.open:focus-visible {
		outline-offset: -2px;
	}

	/* The state dot. Its rules used to live only in SessionRail, whose
	   scoped styles stopped reaching this markup when the row moved out
	   (TD-1715) — the expanded rail drew no dots at all. Same sizes and
	   tones as the collapsed strip's. */
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

	/* A session waiting on an approval breathes (navigation round, 2026-09);
	   without motion the amber and the subtitle's state word carry it. */
	@media (prefers-reduced-motion: no-preference) {
		.dot--attention {
			animation: attention-pulse 1.8s var(--ease-out) infinite;
		}
	}

	@keyframes attention-pulse {
		0%,
		100% {
			box-shadow: 0 0 0 0 color-mix(in srgb, var(--color-warn) 45%, transparent);
		}
		60% {
			box-shadow: 0 0 0 5px transparent;
		}
	}

	.row-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
		line-height: var(--leading-tight);
	}

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

	.row-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		font-family: var(--font-sans);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.row-title--id {
		font-family: var(--font-mono);
	}

	.row-sub {
		font-size: var(--text-xs);
		font-family: var(--font-sans);
		color: var(--color-ink-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.row-state {
		color: var(--color-ink-secondary);
	}

	/* The menu affordance stays out of the way until the row is hovered or the
	   menu is open — a rail of ellipses would read as clutter. */
	.more {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		background: transparent;
		color: var(--color-ink-muted);
		cursor: pointer;
		padding: var(--space-1);
		margin-right: var(--space-1);
		border-radius: var(--radius-sm);
		line-height: 1;
		opacity: 0;
	}

	.row:hover .more,
	.more:focus-visible,
	.more-open {
		opacity: 1;
	}

	.more:hover {
		color: var(--color-ink);
	}

	.menu {
		display: flex;
		flex-direction: column;
		padding: var(--space-1) var(--space-1) var(--space-2);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.action {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		text-align: left;
		border: none;
		background: transparent;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		color: var(--color-ink);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		cursor: pointer;
	}

	.action:hover {
		background: var(--color-ground);
	}

	.action-danger {
		color: var(--color-err);
	}

	.commit {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		padding: var(--space-1) var(--space-2) var(--space-2);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.rename {
		width: 100%;
		box-sizing: border-box;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.rename:focus {
		outline: none;
		border-color: var(--color-accent);
	}

	.commit-copy {
		margin: 0;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		color: var(--color-ink-secondary);
	}

	.commit-btns {
		display: flex;
		gap: var(--space-1);
	}

	.btn {
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn-danger {
		border-color: var(--color-err);
		color: var(--color-err);
	}

	.target {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		text-align: left;
		border: none;
		background: transparent;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		color: var(--color-ink);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		cursor: pointer;
	}

	.target:hover {
		background: var(--color-ground);
	}

	.target-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.refusal {
		margin: 0;
		padding: 0 var(--space-2) var(--space-2);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		color: var(--color-warn);
	}
</style>
