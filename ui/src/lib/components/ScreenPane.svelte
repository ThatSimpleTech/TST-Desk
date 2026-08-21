<script lang="ts">
	// Screen pane (TD-1710, TD-3401): watch the session browser or desktop.
	//
	// Frames arrive as `screen_frame` with a session-dir path. Preview
	// is a data-URL the host read under the same wall as artifacts.
	// TD-3403: one pointer hook — DesignLayer — when Design mode is on.
	import { session } from '../session-status.svelte.js';
	import { screen } from '../screen.svelte.js';
	import { design } from '../design.svelte.js';
	import { SCREEN_EMPTY_COPY, screenTabVisible } from '../screen';
	import DesignLayer from './DesignLayer.svelte';

	let frameEl: HTMLImageElement | null = $state(null);

	let visible = $derived(
		screenTabVisible({
			boundSessionId: screen.boundSessionId,
			sessionId: session.sessionId,
			hasFrame: screen.hasFrame,
			hasCuTool: screen.hasCuTool,
		}),
	);
	let empty = $derived(screen.preview === null && design.frozenPreview === null);
	let frameSrc = $derived(
		design.enabled && design.frozenPreview !== null ? design.frozenPreview : screen.preview,
	);
</script>

<div class="screen-pane">
	{#if !visible || empty || frameSrc === null}
		<p class="empty">{SCREEN_EMPTY_COPY}</p>
		{#if screen.error !== null}
			<p class="error">{screen.error}</p>
		{/if}
	{:else}
		{#if design.enabled}
			<p class="mode" aria-live="polite">Design — click to select</p>
		{/if}
		<div class="frame-wrap">
			<img bind:this={frameEl} class="frame" src={frameSrc} alt="Computer-use screen" />
			<DesignLayer image={frameEl} />
		</div>
	{/if}
</div>

<style>
	.screen-pane {
		height: 100%;
		overflow: hidden;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		background: var(--color-bg);
	}

	.empty {
		padding: var(--space-6);
		color: var(--color-text-muted);
		font-size: var(--text-sm);
		line-height: var(--leading-relaxed);
		text-align: center;
		max-width: 28rem;
	}

	.error {
		padding: 0 var(--space-6);
		color: var(--color-danger);
		font-size: var(--text-sm);
	}

	.mode {
		margin: 0;
		padding: var(--space-2) var(--space-4) 0;
		color: var(--color-accent);
		font-size: var(--text-xs);
		flex-shrink: 0;
	}

	.frame-wrap {
		position: relative;
		max-width: 100%;
		max-height: 100%;
		min-height: 0;
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.frame {
		max-width: 100%;
		max-height: 100%;
		object-fit: contain;
	}
</style>
