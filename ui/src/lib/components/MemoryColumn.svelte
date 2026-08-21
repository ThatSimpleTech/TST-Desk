<script lang="ts">
	// Memory column on the project home (TD-2601 / TD-2602).
	//
	// Lists .tst/memory/*.md. Click shows the markdown. Edit/save is a
	// human-path client message — never a tool, never a steering write.
	import Markdown from './chat/Markdown.svelte';
	import MemoryProposalCard from './MemoryProposalCard.svelte';
	import { memoryEmptyCopy, memoryLocalCopy } from '../memory-files';
	import {
		beginEdit,
		cancelEdit,
		loadMemoryFiles,
		memoryFiles,
		saveMemoryFile,
		selectMemoryFile,
		selectedMemoryFile,
		setMemoryDraft,
		startMemoryFiles,
	} from '../memory-files.svelte.js';
	import { pendingForWorkspace } from '../memory-proposal-store.svelte.js';
	import { sessions } from '../sessions.svelte.js';

	let { workspacePath }: { workspacePath: string } = $props();

	startMemoryFiles();

	$effect(() => {
		loadMemoryFiles(workspacePath);
	});

	let empty = $derived(memoryFiles.files.length === 0);
	let selected = $derived(selectedMemoryFile());
	let dirty = $derived(selected !== null && memoryFiles.draft !== selected.content);
	let proposals = $derived(pendingForWorkspace(workspacePath, sessions.rows));
</script>

<section class="col" aria-label="Memory">
	<h2 class="section">Memory</h2>
	<p class="lede">{memoryLocalCopy()}</p>
	{#if proposals.length > 0}
		<div class="proposals" aria-label="Memory proposal">
			{#each proposals as proposal (proposal.proposalId)}
				<MemoryProposalCard {proposal} />
			{/each}
		</div>
	{/if}
	{#if empty}
		<p class="empty">{memoryEmptyCopy()}</p>
	{:else}
		<ul class="list">
			{#each memoryFiles.files as file (file.path)}
				<li>
					<button
						class="file"
						class:file--open={memoryFiles.selectedPath === file.path}
						type="button"
						onclick={() => selectMemoryFile(file.path)}
					>
						<span class="name">{file.name}</span>
					</button>
				</li>
			{/each}
		</ul>
		{#if selected !== null}
			<div class="preview" aria-label={`Markdown for ${selected.name}`}>
				{#if memoryFiles.editing}
					<textarea
						class="editor"
						aria-label={`Edit ${selected.name}`}
						value={memoryFiles.draft}
						oninput={(e) => setMemoryDraft(e.currentTarget.value)}
					></textarea>
					<div class="actions">
						<button
							class="text-btn"
							type="button"
							disabled={!dirty || memoryFiles.saving}
							onclick={() => saveMemoryFile()}
						>Save</button>
						<button class="text-btn" type="button" onclick={() => cancelEdit()}>Cancel</button>
					</div>
					{#if memoryFiles.error !== null}
						<p class="err">{memoryFiles.error}</p>
					{/if}
				{:else}
					<Markdown text={selected.content} />
					<div class="actions">
						<button class="text-btn" type="button" onclick={() => beginEdit()}>Edit</button>
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</section>

<style>
	.col {
		min-width: 0;
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

	.lede {
		margin: var(--space-2) 0 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.proposals {
		margin-top: var(--space-3);
	}

	.empty {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
	}

	.list {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.file {
		display: flex;
		align-items: baseline;
		width: 100%;
		text-align: left;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
		cursor: pointer;
		color: var(--color-ink);
	}

	.file:hover,
	.file--open {
		background: var(--color-sunken);
	}

	.name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
	}

	.preview {
		margin-top: var(--space-3);
		padding: var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		max-height: 20rem;
		overflow-y: auto;
	}

	.editor {
		display: block;
		width: 100%;
		box-sizing: border-box;
		min-height: 10rem;
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-sunken);
		color: var(--color-ink);
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		resize: vertical;
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		margin-top: var(--space-2);
	}

	.text-btn {
		border: none;
		background: transparent;
		color: var(--color-accent);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
		padding: var(--space-1);
	}

	.text-btn:disabled {
		color: var(--color-ink-muted);
		cursor: default;
	}

	.err {
		margin: var(--space-2) 0 0;
		font-size: var(--text-xs);
		color: var(--color-err);
	}
</style>
