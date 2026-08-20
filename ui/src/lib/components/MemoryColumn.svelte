<script lang="ts">
	// Memory column on the project home (TD-2601).
	//
	// Lists .tst/memory/*.md. Click shows the markdown. Writes stay on
	// distill-accept / TD-2602 — this column is read-only.
	import Markdown from './chat/Markdown.svelte';
	import { memoryEmptyCopy } from '../memory-files';
	import {
		loadMemoryFiles,
		memoryFiles,
		selectMemoryFile,
		selectedMemoryFile,
		startMemoryFiles,
	} from '../memory-files.svelte.js';

	let { workspacePath }: { workspacePath: string } = $props();

	startMemoryFiles();

	$effect(() => {
		loadMemoryFiles(workspacePath);
	});

	let empty = $derived(memoryFiles.files.length === 0);
	let selected = $derived(selectedMemoryFile());
</script>

<section class="col" aria-label="Memory">
	<h2 class="section">Memory</h2>
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
				<Markdown text={selected.content} />
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
</style>
