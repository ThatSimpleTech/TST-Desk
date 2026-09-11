<script lang="ts">
	import EmptyState from './EmptyState.svelte';
	import { grok } from '../grok.svelte.js';
	import { isTauri, openInEditor } from '../open-file';
	import { showRightPane } from '../right-pane.svelte.js';

	let src = $state<string | null>(null);

	$effect(() => {
		const preview = grok.preview;
		src = null;
		if (preview === null) return;
		if (preview.url) {
			src = preview.url;
			return;
		}
		if (preview.path && isTauri()) {
			void loadSrc(preview.path);
		}
	});

	async function loadSrc(path: string): Promise<void> {
		try {
			const { convertFileSrc } = await import('@tauri-apps/api/core');
			src = convertFileSrc(path);
		} catch {
			src = null;
		}
	}

	function openFile(): void {
		const path = grok.preview?.path;
		if (path) void openInEditor(path);
	}
</script>

<div class="preview">
	{#if grok.preview === null}
		<EmptyState
			body="Previews land here when Grok writes an image, video, PDF, HTML file, or starts a loopback server. Switch to this tab from Activity, or they open automatically."
		/>
	{:else}
		<header class="head">
			<span class="kind">{grok.preview.kind}</span>
			<span class="title" title={grok.preview.path ?? grok.preview.url ?? ''}>
				{grok.preview.title || grok.preview.kind}
			</span>
			{#if grok.preview.path}
				<button class="open" type="button" onclick={openFile}>Open</button>
			{/if}
			<button class="open" type="button" onclick={() => showRightPane('activity')}>Hide</button>
		</header>
		<div class="body">
			{#if grok.preview.kind === 'image' && src}
				<img src={src} alt={grok.preview.title || 'Generated image'} />
			{:else if grok.preview.kind === 'video' && src}
				<!-- A clip the agent just wrote to disk has no caption track to offer. -->
				<!-- svelte-ignore a11y_media_has_caption -->
				<video src={src} controls></video>
			{:else if (grok.preview.kind === 'html' || grok.preview.kind === 'pdf' || grok.preview.kind === 'url') && src}
				<iframe title={grok.preview.title || 'Preview'} src={src}></iframe>
			{:else}
				<EmptyState align="start">
					{grok.preview.path ?? grok.preview.url}
					{#if !isTauri()}
						— open the desktop app to preview local files.
					{/if}
				</EmptyState>
			{/if}
		</div>
	{/if}
</div>

<style>
	.preview {
		height: 100%;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.head {
		display: flex;
		gap: var(--space-2);
		align-items: center;
		padding: var(--space-2) var(--space-3);
		border-bottom: 1px solid var(--color-hairline);
		font-size: var(--text-sm);
	}
	.kind {
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--color-ink-muted);
		font-size: 0.75rem;
	}
	.title {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.open {
		background: none;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		color: inherit;
		padding: 2px 8px;
		cursor: pointer;
	}
	.body {
		flex: 1;
		min-height: 0;
		background: var(--color-ground);
	}
	img,
	video,
	iframe {
		width: 100%;
		height: 100%;
		border: 0;
		object-fit: contain;
		background: #111;
	}
</style>
