<script lang="ts">
	// Memory proposal card (TD-2402 / TD-2403): unified diff per file,
	// editable markdown, Accept writes via the store, Reject writes nothing.
	import { onMount } from 'svelte';
	import { classifyDiffLine, diffLines } from '../entry-view';
	import { accept, reject } from '../memory-proposal-store.svelte.js';
	import type { PendingMemoryProposal } from '../memory-proposal';

	interface Props {
		proposal: PendingMemoryProposal;
	}

	let { proposal }: Props = $props();

	let cardEl: HTMLElement | null = null;

	// What the user has typed, per file path — not the whole draft. The
	// proposal supplies the rest, so a card handed a different proposal
	// shows that one's text instead of a snapshot taken at mount. Tagged
	// with the proposal it belongs to; null means nothing typed yet.
	let edits = $state<{ id: string; text: Record<string, string> } | null>(null);

	function typedText(): Record<string, string> {
		return edits !== null && edits.id === proposal.proposalId ? edits.text : {};
	}

	const drafts = $derived.by(() => {
		const typed = typedText();
		return Object.fromEntries(
			proposal.files.map((file) => [file.path, typed[file.path] ?? file.after ?? '']),
		);
	});

	function editDraft(path: string, value: string): void {
		edits = { id: proposal.proposalId, text: { ...typedText(), [path]: value } };
	}

	onMount(() => cardEl?.focus());

	function actionLabel(action: string): string {
		if (action === 'create') return 'Create';
		if (action === 'delete') return 'Delete';
		return 'Replace';
	}

	function acceptDrafts(): void {
		accept(
			proposal,
			proposal.files.map((file) => ({ path: file.path, content: drafts[file.path] ?? '' })),
		);
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
				<label class="editor-label" for="memory-edit-{file.path}">
					Edit {file.path}
					<span class="editor-hint">Empty deletes the file</span>
				</label>
				<textarea
					id="memory-edit-{file.path}"
					class="editor"
					aria-label={`Edit ${file.path}`}
					value={drafts[file.path] ?? ''}
					oninput={(e) => editDraft(file.path, e.currentTarget.value)}
				></textarea>
			</section>
		{/each}
	</div>

	<footer class="card-actions">
		<button class="btn btn--accept" type="button" onclick={acceptDrafts}>Accept</button>
		<button class="btn btn--reject" type="button" onclick={() => reject(proposal)}>Reject</button>
	</footer>
</article>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-left: var(--space-1) solid var(--color-accent);
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
		color: var(--color-ink);
	}

	.badge {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		padding: var(--space-1) var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		color: var(--color-ink-secondary);
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

	.file-action--create { color: var(--color-ok); }
	.file-action--replace { color: var(--color-warn); }
	.file-action--delete { color: var(--color-err); }

	.file-path {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink);
		word-break: break-all;
	}

	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		white-space: pre;
		overflow-x: auto;
	}

	.diff-line { display: inline; }
	.diff-line--add { color: var(--color-ok); }
	.diff-line--del { color: var(--color-err); }
	.diff-line--hunk { color: var(--color-accent); }
	.diff-line--meta { color: var(--color-ink-muted); font-weight: var(--weight-semibold); }
	.diff-line--context { color: var(--color-ink-secondary); }

	.editor-label {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-ink-secondary);
	}

	.editor-hint {
		font-weight: var(--weight-medium);
		color: var(--color-ink-muted);
	}

	.editor {
		min-height: var(--space-16);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		background: var(--color-ground);
		color: var(--color-ink);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		resize: vertical;
	}

	.editor:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

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
		color: var(--color-on-accent);
	}

	.btn--accept:hover {
		background: var(--color-accent-hover);
	}

	.btn--reject {
		background: transparent;
		color: var(--color-err);
		border-color: var(--color-err);
	}

	.btn--reject:hover {
		background: var(--color-err);
		color: var(--color-on-accent);
	}
</style>
