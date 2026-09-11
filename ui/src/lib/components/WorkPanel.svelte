<script lang="ts">
	// Work pane (TD-3203): session writes as a reviewable stack.
	//
	// Not the Files fold (one row per path) and not an in-app editor. Each
	// write is a row — path, +/-, expand the diff. Click opens the OS editor.
	import EmptyState from './EmptyState.svelte';
	import { entries } from '../timeline-store.svelte.js';
	import { baseName, dirName } from '../files';
	import { stackSessionWrites, WORK_EMPTY_COPY } from '../work';
	import { openInEditor } from '../open-file';
	import DiffPreview from './DiffPreview.svelte';
	import Icon from './Icon.svelte';

	let stack = $derived(stackSessionWrites(entries));
	let expanded = $state<string | null>(null);

	function toggle(id: string): void {
		expanded = expanded === id ? null : id;
	}

	function open(path: string): void {
		void openInEditor(path);
	}
</script>

<div class="work-panel">
	{#if stack.writes.length === 0}
		<EmptyState icon="pencil" body={WORK_EMPTY_COPY} />
	{:else}
		<ul class="writes">
			{#each stack.writes as write (write.id)}
				{@const isOpen = expanded === write.id}
				<li class="write">
					<div class="row">
						<button
							class="name"
							type="button"
							onclick={() => open(write.path)}
							title={`Open ${write.path}`}
						>
							{baseName(write.path)}
						</button>
						<span class="counts">
							<span class="added">+{write.added}</span>
							<span class="removed">-{write.removed}</span>
						</span>
						<button
							class="chevron"
							type="button"
							aria-expanded={isOpen}
							aria-label={`Diff for ${write.path}`}
							onclick={() => toggle(write.id)}
						>
							<span class="chevron-icon" class:chevron-icon--open={isOpen} aria-hidden="true">
								<Icon name="chevron-right" size={14} />
							</span>
						</button>
					</div>
					<div class="meta">
						{#if dirName(write.path)}
							<span class="dir" title={write.path}>{dirName(write.path)}</span>
						{/if}
						<span class="tool">{write.tool ?? 'write'}</span>
					</div>
					{#if isOpen}
						<div class="diff">
							<DiffPreview diff={write.diff} />
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.work-panel {
		height: 100%;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
	}

	.writes {
		flex: 1;
		list-style: none;
		margin: 0;
		padding: var(--space-2) 0;
	}

	.write {
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

	/* Same disclosure control as the timeline rows and the Files pane. */
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

	.dir {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.tool {
		white-space: nowrap;
	}

	.diff {
		margin-top: var(--space-2);
		animation: rise var(--dur-enter) var(--ease-out);
	}
</style>
