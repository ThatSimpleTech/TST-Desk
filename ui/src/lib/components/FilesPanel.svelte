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
	import { entries } from '../timeline-store.svelte.js';
	import { baseName, dirName, foldFileWrites } from '../files';
	import { classifyDiffLine, diffLines } from '../entry-view';
	import { openInEditor } from '../open-file';

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
		<p class="empty">
			No files written yet. Every file this session writes appears here — its diff, its line
			counts, and a click to open it in your editor.
		</p>
	{:else}
		<ul class="files">
			{#each summary.files as file (file.path)}
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
							aria-expanded={expanded === file.path}
							aria-label={`Diff for ${file.path}`}
							onclick={() => toggle(file.path)}>{expanded === file.path ? '▾' : '▸'}</button
						>
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
					{#if expanded === file.path}
						<div class="diffs">
							<!-- Unkeyed: the list only ever appends, and one result can
							     name the same path twice (a move onto itself), so seq is
							     not unique within a file. -->
							{#each file.writes as write}
								<section class="write">
									<h4 class="write-title">{write.tool ?? 'write'} · +{write.added} -{write.removed}</h4>
									<pre class="code diff">{#each diffLines(write.diff) as line}<span class="diff-line diff-line--{classifyDiffLine(line)}">{line}</span>{'\n'}{/each}</pre>
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

	.empty {
		padding: var(--space-6);
		color: var(--color-text-muted);
		font-size: var(--text-sm);
		line-height: var(--leading-relaxed);
		text-align: center;
	}

	.files {
		flex: 1;
		list-style: none;
		margin: 0;
		padding: var(--space-2) 0;
	}

	.file {
		padding: var(--space-2) var(--space-4);
		border-bottom: var(--border-width) solid var(--color-border);
	}

	.row {
		display: flex;
		align-items: baseline;
		gap: var(--space-3);
	}

	.name {
		flex: 1;
		min-width: 0;
		padding: 0;
		border: none;
		background: none;
		color: var(--color-text);
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
		white-space: nowrap;
	}

	.added {
		color: var(--color-success);
	}

	.removed {
		color: var(--color-danger);
	}

	.chevron {
		padding: 0;
		border: none;
		background: none;
		color: var(--color-text-muted);
		font-size: var(--text-xs);
		line-height: 1;
		cursor: pointer;
	}

	.chevron:hover {
		color: var(--color-text);
	}

	.meta {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		margin-top: var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-text-muted);
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
		color: var(--color-text-secondary);
	}

	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-bg);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		overflow-x: auto;
	}

	/* Same treatment as the timeline's expanded write (TD-1005 AC #3). */
	.diff {
		white-space: pre;
	}

	.diff-line {
		display: inline;
	}
	.diff-line--add {
		color: var(--color-success);
	}
	.diff-line--del {
		color: var(--color-danger);
	}
	.diff-line--hunk {
		color: var(--color-info);
	}
	.diff-line--meta {
		color: var(--color-text-muted);
		font-weight: var(--weight-semibold);
	}
	.diff-line--context {
		color: var(--color-text-secondary);
	}

	.totals {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-border);
		font-size: var(--text-sm);
		color: var(--color-text-secondary);
		position: sticky;
		bottom: 0;
		background: var(--color-bg-raised);
	}
</style>
