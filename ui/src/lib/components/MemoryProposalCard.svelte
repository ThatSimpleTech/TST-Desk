<script lang="ts">
	// Memory proposal card (TD-2402): unified diff per file, Accept writes
	// via the parked proposal (TD-2303 / TD-2104), Reject writes nothing.
	// Presentational — the store owns bind-and-clear and the wire verbs.
	import { onMount } from 'svelte';
	import { classifyDiffLine, diffLines } from '../entry-view';
	import { accept, reject } from '../memory-proposal-store.svelte.js';
	import type { PendingMemoryProposal } from '../memory-proposal';

	interface Props {
		proposal: PendingMemoryProposal;
	}

	let { proposal }: Props = $props();

	let cardEl: HTMLElement | null = null;

	onMount(() => cardEl?.focus());

	function actionLabel(action: string): string {
		if (action === 'create') return 'Create';
		if (action === 'delete') return 'Delete';
		return 'Replace';
	}
</script>

<article
	class="card"
	tabindex="-1"
	bind:this={cardEl}
	aria-label="Memory proposal"
>
	<header class="card-head">
		<span class="card-title">Remember this?</span>
		<span class="badge">{proposal.files.length} file{proposal.files.length === 1 ? '' : 's'}</span>
	</header>

	<div class="files">
		{#each proposal.files as file (file.path)}
			<section class="file">
				<header class="file-head">
					<span class="file-action file-action--{file.action}">{actionLabel(file.action)}</span>
					<span class="file-path">{file.path}</span>
				</header>
				<pre class="code diff" aria-label={`Diff for ${file.path}`}>{#each diffLines(file.diff) as line}<span class="diff-line diff-line--{classifyDiffLine(line)}">{line}</span>{'\n'}{/each}</pre>
			</section>
		{/each}
	</div>

	<footer class="card-actions">
		<button class="btn btn--accept" type="button" onclick={() => accept(proposal)}>Accept</button>
		<button class="btn btn--reject" type="button" onclick={() => reject(proposal)}>Reject</button>
	</footer>
</article>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-bg);
		border: var(--border-width) solid var(--color-border);
		border-left: var(--space-1) solid var(--color-info);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-sm);
	}

	.card:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 2px;
	}

	.card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
	}

	.card-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-text);
	}

	.badge {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		padding: var(--space-1) var(--space-2);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-full);
		color: var(--color-text-secondary);
		flex-shrink: 0;
	}

	.files {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.file {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.file-head {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
	}

	.file-action {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		flex-shrink: 0;
	}

	.file-action--create { color: var(--color-success); }
	.file-action--replace { color: var(--color-warning); }
	.file-action--delete { color: var(--color-danger); }

	.file-path {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-text);
		word-break: break-all;
	}

	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-bg-subtle);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		white-space: pre;
		overflow-x: auto;
	}

	.diff-line { display: inline; }
	.diff-line--add { color: var(--color-success); }
	.diff-line--del { color: var(--color-danger); }
	.diff-line--hunk { color: var(--color-info); }
	.diff-line--meta { color: var(--color-text-muted); font-weight: var(--weight-semibold); }
	.diff-line--context { color: var(--color-text-secondary); }

	.card-actions {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-2);
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-4);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-md);
		cursor: pointer;
	}

	.btn--accept {
		background: var(--color-accent);
		color: var(--color-accent-text);
	}

	.btn--accept:hover {
		background: var(--color-accent-hover);
	}

	.btn--reject {
		background: transparent;
		color: var(--color-danger);
		border-color: var(--color-danger);
	}

	.btn--reject:hover {
		background: var(--color-danger);
		color: var(--color-accent-text);
	}
</style>
