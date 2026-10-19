<script lang="ts">
	// Steering proposal card (TD-5140): the agent's proposed edit to a
	// steering file as a diff. Accept, or Edit then Save, is the only thing
	// that writes it, and the daemon does the write; Reject writes nothing.
	import { onMount } from 'svelte';
	import { classifyDiffLine, diffLines } from '../entry-view';
	import { accept, reject, saveEdit } from '../steering-proposal-store.svelte.js';
	import { actionTitle, type PendingSteeringProposal } from '../steering-proposal';

	interface Props {
		proposal: PendingSteeringProposal;
	}

	let { proposal }: Props = $props();

	let cardEl: HTMLElement | null = null;

	// The draft belongs to the proposal it was opened on: a fresh diff after
	// a stale Accept is a different proposal and starts from its own text.
	let editing = $state<{ id: string; text: string } | null>(null);
	const draft = $derived(editing !== null && editing.id === proposal.proposalId ? editing : null);

	onMount(() => cardEl?.focus());

	function startEdit(): void {
		editing = { id: proposal.proposalId, text: proposal.file.after ?? '' };
	}

	function save(): void {
		if (draft === null) return;
		saveEdit(proposal, draft.text);
	}
</script>

<article class="card" tabindex="-1" bind:this={cardEl} aria-label="Steering edit proposal">
	<header class="card-head">
		<span class="card-title">The agent proposes a steering edit</span>
		{#if proposal.autonomy}
			<span class="badge">From an unattended run</span>
		{/if}
	</header>

	<p class="file-head">
		<span class="file-action file-action--{proposal.file.action}">{actionTitle(proposal.file)}</span>
	</p>

	{#if draft === null}
		<pre class="code diff" aria-label={`Diff for ${proposal.file.path}`}>{#each diffLines(proposal.file.diff) as line}<span class="diff-line diff-line--{classifyDiffLine(line)}">{line}</span>{'\n'}{/each}</pre>
	{:else}
		<label class="editor-label" for="steering-edit-{proposal.proposalId}">Edit {proposal.file.path}</label>
		<textarea
			id="steering-edit-{proposal.proposalId}"
			class="editor"
			aria-label={`Edit ${proposal.file.path}`}
			value={draft.text}
			oninput={(e) => (editing = { id: proposal.proposalId, text: e.currentTarget.value })}
		></textarea>
	{/if}

	{#if proposal.notice}
		<p class="notice" role="status">{proposal.notice}</p>
	{/if}

	<footer class="card-actions">
		{#if draft === null}
			<button class="btn btn--accept" type="button" onclick={() => accept(proposal)}>Accept</button>
			<button class="btn btn--quiet" type="button" onclick={startEdit}>Edit</button>
			<button class="btn btn--reject" type="button" onclick={() => reject(proposal)}>Reject</button>
		{:else}
			<button class="btn btn--accept" type="button" onclick={save}>Save</button>
			<button class="btn btn--quiet" type="button" onclick={() => (editing = null)}>Cancel</button>
		{/if}
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
		border-left: var(--space-1) solid var(--color-warn);
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

	.file-head {
		margin: 0;
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		word-break: break-all;
	}

	.file-action {
		font-weight: var(--weight-semibold);
	}

	.file-action--create { color: var(--color-ok); }
	.file-action--replace { color: var(--color-warn); }

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
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-ink-secondary);
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

	.notice {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-warn);
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

	.btn--quiet {
		background: transparent;
		color: var(--color-ink);
		border-color: var(--color-hairline);
	}

	.btn--quiet:hover {
		background: var(--color-sunken);
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
