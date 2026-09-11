<script lang="ts">
	// Artifacts rail surface (TD-3202).
	//
	// Flat list of the bound session's artifacts. Click opens a preview —
	// markdown, highlighted code (the chat stack, not Monaco), or HTML in
	// a network-blocked sandbox. Copy is the source. Open in editor is
	// only a workspace path. Not a file tree; no apply/reject.
	import EmptyState from './EmptyState.svelte';
	import Markdown from './chat/Markdown.svelte';
	import {
		asFencedMarkdown,
		artifactsEmptyCopy,
		copyArtifactSource,
		htmlPreviewSrcdoc,
		HTML_PREVIEW_SANDBOX,
		openArtifactInEditor,
		workspaceOpenPath,
	} from '../artifacts';
	import { artifacts, selectArtifact } from '../artifacts.svelte.js';

	let empty = $derived(artifactsEmptyCopy(artifacts.sessionId !== null));
	let preview = $derived(artifacts.preview);
	let canOpen = $derived(
		preview !== null &&
			workspaceOpenPath(artifacts.workspacePath, preview.path, preview.artifactId) !== null,
	);

	function sandboxFrame(node: HTMLIFrameElement): void {
		// Empty sandbox must stay on the element. Svelte can drop an empty
		// string attribute; a missing sandbox is an unsandboxed iframe.
		node.setAttribute('sandbox', HTML_PREVIEW_SANDBOX);
	}

	async function copySource(): Promise<void> {
		if (preview?.source == null) return;
		await copyArtifactSource(preview.source);
	}

	async function openInOs(): Promise<void> {
		if (preview === null) return;
		await openArtifactInEditor(artifacts.workspacePath, preview.path, preview.artifactId);
	}
</script>

<div class="pane">
	<section class="list-col" aria-label="Artifacts">
		<h1 class="title">Artifacts</h1>
		<p class="lede">Products of the bound session. Not the files it wrote along the way.</p>
		{#if artifacts.items.length === 0}
			<EmptyState align="start" body={empty} />
		{:else}
			<ul class="list">
				{#each artifacts.items as row (row.artifact_id)}
					<li>
						<button
							class="card"
							class:card-current={artifacts.selectedId === row.artifact_id}
							type="button"
							onclick={() => selectArtifact(row.artifact_id)}
						>
							<span class="card-name">{row.title}</span>
							<span class="card-meta">{row.mime}</span>
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
	<section class="preview-col" aria-label="Artifact preview">
		{#if preview === null}
			<EmptyState
				compact
				align="start"
				body={artifacts.loading ? 'Opening…' : 'Select an artifact to preview.'}
			/>
		{:else}
			<header class="preview-head">
				<h2 class="preview-title">{preview.title}</h2>
				<div class="actions">
					<button
						class="action"
						type="button"
						disabled={preview.source === null}
						onclick={() => void copySource()}
					>
						Copy source
					</button>
					{#if canOpen}
						<button class="action" type="button" onclick={() => void openInOs()}>
							Open in editor
						</button>
					{/if}
				</div>
			</header>
			{#if artifacts.error !== null}
				<p class="error">{artifacts.error}</p>
			{:else if preview.source === null}
				<EmptyState compact align="start" body="Opening…" />
			{:else if preview.kind === 'html'}
				<iframe
					class="html-frame"
					title={preview.title}
					sandbox={HTML_PREVIEW_SANDBOX}
					srcdoc={htmlPreviewSrcdoc(preview.source)}
					use:sandboxFrame
				></iframe>
			{:else if preview.kind === 'markdown'}
				<div class="md"><Markdown text={preview.source} /></div>
			{:else}
				<div class="md"><Markdown text={asFencedMarkdown(preview.source, preview.path)} /></div>
			{/if}
		{/if}
	</section>
</div>

<style>
	.pane {
		display: flex;
		height: 100%;
		min-height: 0;
		background: var(--color-ground);
	}

	.list-col,
	.preview-col {
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	.list-col {
		flex: 0 0 18rem;
		padding: var(--space-8) var(--space-6);
		border-right: var(--border-width) solid var(--color-hairline);
	}

	.preview-col {
		flex: 1;
		padding: var(--space-8) var(--space-6);
		display: flex;
		flex-direction: column;
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

	.error {
		margin: var(--space-4) 0 0;
		font-size: var(--text-sm);
		color: var(--color-err);
	}

	.list {
		list-style: none;
		margin: var(--space-6) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.card {
		display: flex;
		flex-direction: column;
		gap: 1px;
		width: 100%;
		text-align: left;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		padding: var(--space-3) var(--space-4);
		cursor: pointer;
		color: var(--color-ink);
	}

	.card:hover,
	.card-current {
		background: var(--color-sunken);
	}

	.card-name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
	}

	.card-meta {
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
	}

	.preview-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-4);
	}

	.preview-title {
		margin: 0;
		font-family: var(--font-display);
		font-size: var(--text-xl);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-ink);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		flex-shrink: 0;
	}

	.action {
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		color: var(--color-ink);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-3);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.action:hover:not(:disabled) {
		background: var(--color-sunken);
	}

	.action:disabled {
		color: var(--color-ink-muted);
		cursor: default;
	}

	.md {
		margin-top: var(--space-6);
		min-height: 0;
	}

	.html-frame {
		flex: 1;
		width: 100%;
		min-height: 16rem;
		margin-top: var(--space-6);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-lifted);
	}
</style>
