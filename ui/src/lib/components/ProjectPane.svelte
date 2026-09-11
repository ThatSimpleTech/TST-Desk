<script lang="ts">
	// Project list and project home (TD-2801).
	//
	// Rail Projects lands here instead of the title-bar recents menu.
	// Selecting a row shows a home: folder name, New chat (`new_session`
	// in that workspace), and recents filtered to that path. Columns
	// Context is a later story. Memory is the project-home column (TD-2601),
	// not a rail row.
	import EmptyState from './EmptyState.svelte';
	import Icon from './Icon.svelte';
	import { workspaceName } from '../session-status.svelte.js';
	import { workspaces } from '../workspaces.svelte.js';
	import {
		projects,
		selectProject,
		showHome,
		showProjects,
	} from '../projects.svelte.js';
	import {
		pinnedProjects,
		projectListEmptyCopy,
		projectRecentsEmptyCopy,
		projectSessions,
		unpinnedRecents,
	} from '../projects';
	import { setWorkspacePin } from '../workspaces.svelte.js';
	import {
		ACTIVITY_LABELS,
		activityTone,
		liveActivity,
		newSessionInWorkspace,
		recencyLabel,
		rowTitle,
		rowTitleFull,
		selectRow,
		sessions,
	} from '../sessions.svelte.js';
	import { toggleArchivedView } from '../session-actions.svelte.js';
	import { archivedToggle } from '../rail';
	import InstructionsColumn from './InstructionsColumn.svelte';
	import MemoryColumn from './MemoryColumn.svelte';
	import ContextColumn from './ContextColumn.svelte';
	import CharterColumn from './CharterColumn.svelte';

	let known = $derived(workspaces.entries);
	let pinned = $derived(pinnedProjects(workspaces.entries, workspaces.pinned));
	let recentsList = $derived(unpinnedRecents(workspaces.entries, workspaces.pinned));
	let selected = $derived(projects.selectedPath);
	let recents = $derived(
		selected === null
			? []
			: projectSessions(sessions.rows, selected, { archived: sessions.showArchived }),
	);
	let shelf = $derived(archivedToggle(sessions.showArchived));

	function openRecent(sessionId: string): void {
		selectRow(sessionId);
		showHome();
	}
</script>

<div class="pane">
	<div class="column">
		{#if selected === null}
			<h1 class="title">Projects</h1>
			<p class="lede">Workspaces this window has opened. The folder is the project.</p>
			{#if known.length === 0 && pinned.length === 0}
				<EmptyState align="start" body={projectListEmptyCopy()} />
			{:else}
				{#if pinned.length > 0}
					<h2 class="section">Pinned</h2>
					<ul class="list">
						{#each pinned as entry (entry.path)}
							<li class="row">
								<button
									class="card"
									type="button"
									onclick={() => selectProject(entry.path)}
								>
									<span class="card-icon" aria-hidden="true"><Icon name="folder" size={16} /></span>
									<span class="card-text">
										<span class="card-name">{workspaceName(entry.path)}</span>
										<span class="card-path">{entry.path}</span>
									</span>
								</button>
								<button
									class="pin pin--on"
									type="button"
									aria-label="Unpin project"
									onclick={() => setWorkspacePin(entry.path, false)}
								>Unpin</button>
							</li>
						{/each}
					</ul>
				{/if}
				<h2 class="section recents-label">Recents</h2>
				{#if recentsList.length > 0}
					<ul class="list">
						{#each recentsList as entry (entry.path)}
							<li class="row">
								<button
									class="card"
									type="button"
									onclick={() => selectProject(entry.path)}
								>
									<span class="card-icon" aria-hidden="true"><Icon name="folder" size={16} /></span>
									<span class="card-text">
										<span class="card-name">{workspaceName(entry.path)}</span>
										<span class="card-path">{entry.path}</span>
									</span>
								</button>
								<button
									class="pin"
									type="button"
									aria-label="Pin project"
									onclick={() => setWorkspacePin(entry.path, true)}
								>Pin</button>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		{:else}
			<button class="back" type="button" onclick={() => showProjects()}>
				<Icon name="chevron-left" size={14} />
				All projects
			</button>
			<div class="home-head">
				<div class="home-id">
					<h1 class="title">{workspaceName(selected)}</h1>
					<p class="path">{selected}</p>
				</div>
				<button
					class="new-chat"
					type="button"
					onclick={() => {
						if (selected !== null) newSessionInWorkspace(selected);
					}}
				>
					<Icon name="plus" size={14} />
					New chat
				</button>
			</div>
			<div class="home-cols">
				<InstructionsColumn workspacePath={selected} />
				<MemoryColumn workspacePath={selected} />
				<ContextColumn workspacePath={selected} />
				<CharterColumn workspacePath={selected} />
				<section class="col" aria-label="Recents">
					<div class="recents-head">
						<h2 class="section">{sessions.showArchived ? 'Archived' : 'Recents'}</h2>
						<button
							class="shelf"
							type="button"
							aria-pressed={sessions.showArchived}
							aria-label={shelf.hint}
							title={shelf.hint}
							onclick={() => toggleArchivedView()}
						>
							{shelf.label}
						</button>
					</div>
					{#if recents.length === 0}
						<EmptyState align="start" body={projectRecentsEmptyCopy(sessions.showArchived)} />
					{:else}
						<ul class="list">
							{#each recents as row (row.sessionId)}
								<li>
									<button class="card" type="button" onclick={() => openRecent(row.sessionId)}>
										<span
											class="dot dot-{activityTone(liveActivity(row))}"
											class:dot-live={liveActivity(row) === 'working'}
											aria-hidden="true"
										></span>
										<span class="card-text">
											<span class="card-name" title={rowTitleFull(row)}>{rowTitle(row)}</span>
											<span class="card-path"
												>{ACTIVITY_LABELS[liveActivity(row)]} · {recencyLabel(row.updatedAt)}</span
											>
										</span>
									</button>
								</li>
							{/each}
						</ul>
					{/if}
				</section>
			</div>
		{/if}
	</div>
</div>

<style>
	.pane {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--color-ground);
	}

	.column {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		width: min(760px, 100%);
		margin-inline: auto;
		padding: var(--space-8) var(--space-6) var(--space-10);
	}

	.title {
		margin: 0;
		font-family: var(--font-display);
		font-size: var(--text-3xl);
		font-weight: var(--weight-normal);
		letter-spacing: var(--tracking-display);
		line-height: var(--leading-tight);
		color: var(--color-ink);
	}

	.lede {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.list {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.row {
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}

	.pin {
		flex-shrink: 0;
		border: none;
		background: transparent;
		color: var(--color-accent);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		cursor: pointer;
		padding: var(--space-1);
	}

	.recents-label {
		margin-top: var(--space-6);
	}

	.card {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		text-align: left;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		padding: var(--space-3) var(--space-4);
		cursor: pointer;
		color: var(--color-ink);
	}

	.card:hover {
		background: var(--color-sunken);
	}

	.card-icon {
		display: inline-flex;
		color: var(--color-ink-secondary);
		flex-shrink: 0;
	}

	.card-text {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.card-name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
	}

	.card-path {
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.back {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		margin: 0 0 var(--space-4);
		padding: 0;
		border: none;
		background: transparent;
		color: var(--color-ink-secondary);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.back:hover {
		color: var(--color-ink);
	}

	.home-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-4);
	}

	.home-id {
		min-width: 0;
	}

	.path {
		margin: var(--space-2) 0 0;
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.new-chat {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		flex-shrink: 0;
		border: none;
		background: var(--color-accent);
		color: var(--color-on-accent);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.new-chat:hover {
		background: var(--color-accent-hover);
	}

	.home-cols {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-8);
		margin-top: var(--space-8);
	}

	.col {
		flex: 1 1 16rem;
		min-width: 0;
	}

	.recents-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-2);
	}

	.section {
		margin: 0;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--color-ink-muted);
	}

	.shelf {
		border: none;
		background: transparent;
		color: var(--color-accent);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		cursor: pointer;
		padding: 0;
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
</style>
