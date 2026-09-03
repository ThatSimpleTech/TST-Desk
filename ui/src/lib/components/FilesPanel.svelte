<script lang="ts">
	// Files pane (TD-1705).
	//
	// Presentational view over the per-file fold in files.ts: the session's
	// writes, grouped by the path each diff names, with the diffs that made
	// them and a running total across all of them. No new protocol — the
	// diffs already ride the tool_result events the timeline store holds.
	//
	// The fold re-runs when the timeline changes, which only costs anything
	// while this tab is the visible one; the alternative, a second store
	// subscribed to the same event stream, would keep two copies of the
	// session's writes in step for no gain.
	import EmptyState from './EmptyState.svelte';
	import { entries } from '../timeline-store.svelte.js';
	import { baseName, dirName, foldFileWrites } from '../files';
	import { openInEditor } from '../open-file';
	import DiffPreview from './DiffPreview.svelte';
	import Icon from './Icon.svelte';

	let summary = $derived(foldFileWrites(entries));
	let expanded = $state<string | null>(null);

	function toggle(path: string): void {
		expanded = expanded === path ? null : path;
	}

	// Paths come from the daemon's own diff headers — canonical, absolute,
	// never free text — so handing one to the OS opener is safe (TD-1201).
	function open(path: string): void {
		void openInEditor(path);
	}
</script>

<div class="files-panel">
	{#if summary.files.length === 0}
		<EmptyState
			icon="file"
			title="No files written yet"
			body="Every file this session writes appears here — its diff, its line counts, and a click to open it in your editor."
		/>
	{:else}
		<ul class="files">
			{#each summary.files as file (file.path)}
				{@const isOpen = expanded === file.path}
				<li class="file">
					<div class="row">
						<button
							class="name"
							type="button"
							onclick={() => open(file.path)}
							title={`Open ${file.path}`}
						>
							{baseName(file.path)}
						</button>
						<span class="counts">
							<span class="added">+{file.added}</span>
							<span class="removed">-{file.removed}</span>
						</span>
						<button
							class="chevron"
							type="button"
							aria-expanded={isOpen}
							aria-label={`Diff for ${file.path}`}
							onclick={() => toggle(file.path)}
						>
							<span class="chevron-icon" class:chevron-icon--open={isOpen} aria-hidden="true">
								<Icon name="chevron-right" size={14} />
							</span>
						</button>
					</div>
					<div class="meta">
						{#if dirName(file.path)}
							<span class="dir" title={file.path}>{dirName(file.path)}</span>
						{/if}
						<span class="writes"
							>{file.writes.length}
							{file.writes.length === 1 ? 'write' : 'writes'}</span
						>
					</div>
					{#if isOpen}
						<div class="diffs">
							<!-- Unkeyed: the list only ever appends, and one result can
							     name the same path twice (a move onto itself), so seq is
							     not unique within a file. -->
							{#each file.writes as write}
								<section class="write">
									<h4 class="write-title">{write.tool ?? 'write'} · +{write.added} -{write.removed}</h4>
									<DiffPreview diff={write.diff} />
								</section>
							{/each}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
		<footer class="totals">
			<span>
				{summary.fileCount}
				{summary.fileCount === 1 ? 'file' : 'files'} · {summary.writeCount}
				{summary.writeCount === 1 ? 'write' : 'writes'}
			</span>
			<span class="counts">
				<span class="added">+{summary.added}</span>
				<span class="removed">-{summary.removed}</span>
			</span>
		</footer>
	{/if}
</div>

<style>
	.files-panel {
		height: 100%;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
	}

	/* Same empty treatment as the Activity and Work panes. */
	.files {
		flex: 1;
		list-style: none;
		margin: 0;
		padding: var(--space-2) 0;
	}

	.file {
		padding: var(--space-2) var(--space-4);
		border-bottom: var(--border-width) solid var(--color-hairline);
	}

	.row {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}

	.name {
		flex: 1;
		min-width: 0;
		padding: 0;
		border: none;
		background: none;
		color: var(--color-ink);
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		text-align: left;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		cursor: pointer;
	}

	.name:hover {
		text-decoration: underline;
	}

	.counts {
		display: flex;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.added {
		color: var(--color-ok);
	}

	.removed {
		color: var(--color-err);
	}

	/* Same disclosure control as the timeline rows: an icon chevron that
	   turns to point down when the diff is open. */
	.chevron {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		width: 1.5rem;
		height: 1.5rem;
		padding: 0;
		border: none;
		border-radius: var(--radius-sm);
		background: none;
		color: var(--color-ink-muted);
		cursor: pointer;
	}

	.chevron:hover {
		color: var(--color-ink);
		background: var(--color-sunken);
	}

	.chevron-icon {
		display: inline-flex;
		transition: transform var(--dur-exit) var(--ease-out);
	}

	.chevron-icon--open {
		transform: rotate(90deg);
	}

	.meta {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		margin-top: var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	/* The directory is context, not the identity — it yields the width. The
	   full path stays in the tooltip when it doesn't fit. */
	.dir {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.writes {
		white-space: nowrap;
	}

	.diffs {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		margin-top: var(--space-2);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.write {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.write-title {
		margin: 0;
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-ink-secondary);
	}

	.totals {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		position: sticky;
		bottom: 0;
		background: var(--color-lifted);
	}
</style>
