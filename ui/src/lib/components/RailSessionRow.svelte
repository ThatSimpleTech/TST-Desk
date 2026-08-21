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
		ROW_STATE_LABELS,
		rowSubtitle,
		rowTitle,
		sessions,
		stateTone,
		type SessionRow
	} from '../sessions.svelte.js';
	import {
		confirmDelete,
		moveRow,
		moveTargets,
		requestDelete,
		requestMove,
		setArchived,
		setStarred,
		toggleRowMenu
	} from '../session-actions.svelte.js';

	let { row, active, onselect }: { row: SessionRow; active: boolean; onselect: () => void } =
		$props();

	let menuOpen = $derived(sessions.menuFor === row.sessionId);
	let confirming = $derived(sessions.confirmDeleteFor === row.sessionId);
	let moving = $derived(sessions.moveFor === row.sessionId);
	let targets = $derived(moving ? moveTargets(row.sessionId) : []);

	function run(id: RailRowActionId): void {
		if (id === 'star') setStarred(row.sessionId, true);
		else if (id === 'unstar') setStarred(row.sessionId, false);
		else if (id === 'archive') setArchived(row.sessionId, true);
		else if (id === 'unarchive') setArchived(row.sessionId, false);
		else if (id === 'move') requestMove(row.sessionId);
		else if (id === 'delete') requestDelete(row.sessionId);
	}
</script>

<div class="wrap" class:wrap-open={menuOpen || confirming || moving}>
	<div class="row" class:row-active={active}>
		<button
			class="open"
			type="button"
			aria-current={active ? 'true' : undefined}
			onclick={onselect}
		>
			<span class="dot dot-{stateTone(row.state)}" aria-hidden="true"></span>
			<span class="row-text">
				<span class="row-title">{rowTitle(row)}</span>
				<span class="row-sub">{rowSubtitle(row)} · {ROW_STATE_LABELS[row.state]}</span>
			</span>
		</button>
		<button
			class="more"
			class:more-open={menuOpen}
			type="button"
			title="Session actions"
			aria-label={`Actions for session ${rowTitle(row)}`}
			aria-expanded={menuOpen}
			onclick={() => toggleRowMenu(row.sessionId)}><Icon name="ellipsis" size={14} /></button
		>
	</div>

	{#if menuOpen}
		<div class="menu" role="group" aria-label={`Actions for session ${rowTitle(row)}`}>
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
