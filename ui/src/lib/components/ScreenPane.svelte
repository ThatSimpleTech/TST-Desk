<script lang="ts">
	// Screen pane (TD-1710, TD-3401): watch the session browser or desktop.
	//
	// Frames arrive as `screen_frame` with a session-dir path. Preview
	// is a data-URL the host read under the same wall as artifacts.
	// TD-3402: glow + agent cursor overlays hug the frame, not the pane.
	// TD-3403: DesignLayer + a visible Design control (⌘⇧D also works).
	import EmptyState from './EmptyState.svelte';
	import { session } from '../session-status.svelte.js';
	import { screen } from '../screen.svelte.js';
	import { design, toggleDesign } from '../design.svelte.js';
	import { SCREEN_EMPTY_COPY, screenTabVisible } from '../screen';
	import GlowLayer from './GlowLayer.svelte';
	import AgentCursor from './AgentCursor.svelte';
	import DesignLayer from './DesignLayer.svelte';

	let frameEl: HTMLImageElement | null = $state(null);
	// Natural frame size, read on load. The wrap is sized from it so the
	// overlay (glow, cursor, Design picks) covers exactly the drawn image.
	let natural = $state({ width: 16, height: 10 });

	function onFrameLoad(): void {
		if (frameEl === null || frameEl.naturalWidth === 0 || frameEl.naturalHeight === 0) return;
		natural = { width: frameEl.naturalWidth, height: frameEl.naturalHeight };
	}

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
	let canDesign = $derived(!empty && frameSrc !== null && !design.actuating);

	function onDesign(): void {
		toggleDesign();
	}
</script>

<div class="screen-pane">
	{#if visible && !empty}
		<div class="toolbar">
			{#if design.enabled}
				<p class="mode" aria-live="polite">Design — click to select · shift-click adds · shift-drag a region</p>
			{:else}
				<p class="mode mode--watch">Watch surface</p>
			{/if}
			<button
				class="design"
				class:design--on={design.enabled}
				type="button"
				aria-pressed={design.enabled}
				disabled={!canDesign && !design.enabled}
				title="Design mode (⌘⇧D)"
				onclick={onDesign}
			>
				Design
			</button>
		</div>
	{/if}
	<div class="stage">
		{#if !visible || empty || frameSrc === null}
			<EmptyState body={SCREEN_EMPTY_COPY} />
			{#if screen.error !== null}
				<p class="error">{screen.error}</p>
			{/if}
		{:else}
			<div
				class="frame-wrap"
				style:--frame-ratio="{natural.width} / {natural.height}"
				style:--frame-width="{natural.width}px"
			>
				<img
					bind:this={frameEl}
					class="frame"
					src={frameSrc}
					alt="Computer-use screen"
					onload={onFrameLoad}
				/>
				<div class="overlay">
					<DesignLayer image={frameEl} />
					<GlowLayer />
					<AgentCursor />
				</div>
			</div>
		{/if}
	</div>
</div>

<style>
	.screen-pane {
		height: 100%;
		overflow: hidden;
		display: flex;
		flex-direction: column;
		background: var(--color-ground);
	}

	.toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		flex-shrink: 0;
		padding: var(--space-2) var(--space-3);
		border-bottom: var(--border-width) solid var(--color-hairline);
	}

	.mode {
		margin: 0;
		color: var(--color-accent);
		font-size: var(--text-xs);
		min-width: 0;
	}

	.mode--watch {
		color: var(--color-ink-muted);
	}

	.design {
		flex-shrink: 0;
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
	}

	.design:hover:not(:disabled) {
		border-color: var(--color-accent);
		color: var(--color-accent);
	}

	.design--on {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.design:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.stage {
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		/* The frame sizes itself against this box (cqw/cqh). */
		container-type: size;
	}

	.error {
		padding: 0 var(--space-6);
		color: var(--color-err);
		font-size: var(--text-sm);
	}

	/* Fit the frame inside the stage at its own aspect ratio, never past its
	   natural size. Sized from .stage (a size container), not from the
	   image: WebKit (the macOS app's WKWebView) collapsed the earlier grid of
	   percentage max-sizes to 0×0, so the pane showed only the cursor
	   (TD-5189). */
	.frame-wrap {
		position: relative;
		aspect-ratio: var(--frame-ratio);
		width: min(100cqw, calc(100cqh * (var(--frame-ratio))), var(--frame-width));
	}

	.frame {
		display: block;
		width: 100%;
		height: 100%;
	}

	.overlay {
		position: absolute;
		inset: 0;
		pointer-events: none;
	}
</style>
